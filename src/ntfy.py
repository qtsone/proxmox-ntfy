
import os
import logging
import aiohttp
import asyncio
import proxmoxer
import sys
import urllib3
import time
import json

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Ntfy server details
NTFY_SERVER_URL = os.getenv('NTFY_SERVER_URL', "https://ntfy.sh/CA9FFE70-B1B0-4C1C-9256-0BBD8FAE2CE6")
NTFY_TOKEN = os.getenv('NTFY_TOKEN', None)
NTFY_USER = os.getenv('NTFY_USER', None)
NTFY_PASS = os.getenv('NTFY_PASS', None)

# Create a cache class
class Cache(object):
    """
    Cache Requests
    """
    color_id = 0
    clr = "\033[0m"
    colors = (
        "\033[0m",      # RST
        "\033[31m",     # RED
        "\033[92m",     # GREEN
        "\033[33m",     # YELLOW
        "\033[34m",     # BLUE
        "\033[35m",     # MAGENTA
        "\033[36m",     # CYAN
        "\033[37m",     # WHITE
        "\033[95m",     # VIVID
    )

    def __init__(self):
        Cache.color_id += 1
        if Cache.color_id not in range(1, len(Cache.colors)):
            Cache.color_id = 1

        self.color_id = Cache.color_id

    def color(self):
        return self.colors[self.color_id]

    def clr(self):
        return self.colors[0]


task_handlers = {}
queue = asyncio.Queue()
processed_tasks = set()

async def send_notification(title, tags, message):
    async with aiohttp.ClientSession() as session:
        headers = {
            "Title": title,
            "Tags": tags,
            "Markdown": "yes"
        }

        if NTFY_TOKEN:
            headers['Authorization'] = f'Bearer {NTFY_TOKEN}'
        elif NTFY_USER and NTFY_PASS:
            auth = aiohttp.BasicAuth(NTFY_USER, NTFY_PASS)
        else:
            auth = None
        await session.post(NTFY_SERVER_URL, data=message, headers=headers, auth=auth)


async def get_proxmox_tasks(proxmox, since):
    nodes = proxmox.nodes.get()
    tasks = []
    for node in nodes:
        name = node['node']
        node_tasks = proxmox.nodes(name).tasks.get(since=since, source="all")
        tasks.extend(node_tasks)
    return tasks


async def get_task_status(proxmox, node, task_id):
    status = proxmox.nodes(node).tasks(task_id).status.get()
    logging.debug(f"STATUS [{task_id}] {status}")
    return status


async def get_task_log(proxmox, node, task_id):
    log = proxmox.nodes(node).tasks(task_id).log.get()
    logging.debug(f"LOG [{task_id}] {log}")
    return [log_entry['t'] for log_entry in log if log_entry['t']]


async def monitor_task(proxmox, task):
    task_id = task['upid']
    _, node, uuid, _ = task_id.split(":", maxsplit=3)
    logging.info(f"[{uuid}] Task found. Monitoring...")
    start_time = time.time()
    timeout = 1800
    while True:
        task_status = await get_task_status(proxmox, node, task_id)
        status = task_status.get('status', None)
        exitstatus = task_status.get('exitstatus', None)
        if status == "stopped":
            if exitstatus not in ["OK"]:
                tags = f"warning,{node},{task['type']}"
            else:
                tags = f"white_check_mark,{node},{task['type']}"
            break
        else:
            current_time = time.time()
            elapsed_time = current_time - start_time
            if elapsed_time > timeout:
                tags = f"warning,{node},{task['type']}"
                exitstatus = "TIMEOUT"
                logging.warning(f"TIMEOUT [{uuid}] Timed out after {timeout} seconds.")
                break
            else:
                logging.debug(f"RUNNING [{uuid}] Current status: {status}.")
                await asyncio.sleep(3)

    log_entries = await get_task_log(proxmox, node, task_id)
    title = uuid
    message = f"## Task Details\n\n"
    message += f"**Status**: {exitstatus}\n"
    message += f"**User**: {task['user']}\n\n"

    message += "### Task Status\n"
    message += "```json\n"
    message += json.dumps(task_status, indent=2)
    message += "\n```\n\n"

    message += "### Task Log\n"
    message += "```json\n"
    message += json.dumps(log_entries, indent=2)
    message += "\n```\n"

    await send_notification(title, tags, message)
    logging.info(f"Task {task_id} processed.")
    return task_id


async def fetch_tasks(proxmox):
    logging.info(f'Fetching tasks...')
    current_time = int(time.time())

    while True:
        c = Cache()
        try:
            tasks = await get_proxmox_tasks(proxmox, current_time)

            for task in tasks:
                task_id = task['upid']
                _, _, uuid, _ = task_id.split(":", maxsplit=3)
                if uuid not in processed_tasks:
                    await queue.put(task)
                    processed_tasks.add(uuid)
                    logging.debug(f"Queued task {uuid}")
                    current_time = int(time.time())
            logging.debug(f"Queue Size: {queue.qsize()}.")
        except Exception as e:
            logging.error(f"Error fetching tasks: {e}")

        await asyncio.sleep(10)


async def process_tasks(proxmox):
    """Continually process tasks from the queue."""
    while True:
        c = Cache()
        task = await queue.get()
        task_id = task['upid']
        logging.info(
            c.color() +
            f"Processing {task_id} from queue..." +
            c.clr())

        if not task_handlers.get(task_id):
            task_handler = asyncio.create_task(monitor_task(proxmox, task))
            task_handler.set_name(task_id)
            task_handlers[task_id] = task_handler
            logging.info(f"Started handler for task {task_id}")


async def monitor(proxmox_api='pve',proxmox_user='root@pam', proxmox_pass='root', verify_ssl=False):
    logging.info(f"Monitoring {proxmox_api}...")
    proxmox = proxmoxer.ProxmoxAPI(proxmox_api,
                                   user=proxmox_user,
                                   password=proxmox_pass,
                                   verify_ssl=verify_ssl)

    fetch_task = asyncio.create_task(fetch_tasks(proxmox))
    process_task = asyncio.create_task(process_tasks(proxmox))

    await fetch_task
    await process_task


if __name__ == "__main__":
    log_level = os.getenv('LOG_LEVEL', "DEBUG")
    # Proxmox Details
    proxmox_api = os.getenv('PROXMOX_API_URL', "pve:8006")
    proxmox_user = os.getenv('PROXMOX_USER', "root@pam")
    proxmox_pass = os.getenv('PROXMOX_PASS', "root")

    logging.basicConfig(
        format='%(asctime)s %(levelname)s %(message)s',
        level=log_level,
        stream=sys.stdout)

    asyncio.run(monitor(proxmox_api, proxmox_user, proxmox_pass))
