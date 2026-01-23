import os
import logging
import aiohttp
import asyncio
import proxmoxer
from proxmoxer.core import ResourceException
import sys
import urllib3
import time
import json

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Ntfy server details (validation moved to __main__ after logging setup)
NTFY_SERVER_URL = os.getenv('NTFY_SERVER_URL', None)
NTFY_TOKEN = os.getenv('NTFY_TOKEN', None)
NTFY_USER = os.getenv('NTFY_USER', None)
NTFY_PASS = os.getenv('NTFY_PASS', None)


task_handlers = {}
queue = asyncio.Queue()
processed_tasks = set()


async def send_notification(title, tags, message):
    logging.info(f"Sending notification: Title={title}, Tags={tags}")
    async with aiohttp.ClientSession() as session:
        headers = {
            "Title": title,
            "Tags": tags,
            "Markdown": "yes"
        }

        if NTFY_TOKEN:
            headers['Authorization'] = f'Bearer {NTFY_TOKEN}'
            auth = None
        elif NTFY_USER and NTFY_PASS:
            auth = aiohttp.BasicAuth(NTFY_USER, NTFY_PASS)
        else:
            auth = None
        
        try:
            async with session.post(NTFY_SERVER_URL, data=message, headers=headers, auth=auth) as response:
                logging.debug(f"POST Response: Status={response.status}, Text={await response.text()}")
                response.raise_for_status()  # Raise an exception for 4xx or 5xx status codes
                logging.info(f"Notification sent successfully: Title={title}, Tags={tags}")
        except aiohttp.ClientResponseError as e:
            logging.error(f"Error sending notification: {e}")
            logging.debug(f"Response Headers: {e.headers}")
            logging.debug(f"Response Text: {e.message}")
        except Exception as e:
            logging.error(f"Error sending notification: {e}")

def check_node_permission(permissions, node_name, has_dc_permission=False):
    if has_dc_permission:
        return True
    
    # Check node-specific permissions
    node_path = f"/nodes/{node_name}"
    if node_path in permissions:
        perms = permissions[node_path]
        return has_permission_in_value(perms, 'Sys.Audit')
    
    return False

def has_permission_in_value(perms, perm_name):
    if isinstance(perms, list):
        return perm_name in perms
    elif isinstance(perms, dict):
        # Could be {'Sys.Audit': True} or similar
        return perm_name in perms or any(perm_name in str(p) for p in perms.keys())
    elif isinstance(perms, str):
        return perm_name in perms
    return False

async def check_permissions(proxmox, nodes):
    if not nodes:
        return [], "No nodes found to check permissions"
    
    try:
        # Get the effective permissions for the current user/token
        permissions = proxmox.access.permissions.get()
        logging.debug(f"Retrieved permissions: {permissions}")
        
        # Check if we have Sys.Audit permission at datacenter level (/)
        # This applies to all nodes
        has_dc_permission = False
        if '/' in permissions:
            dc_perms = permissions['/']
            if has_permission_in_value(dc_perms, 'Sys.Audit'):
                has_dc_permission = True
                logging.info("Found Sys.Audit permission at datacenter level (applies to all nodes)")
        
        # Check each node individually
        allowed_nodes = []
        excluded_nodes = []
        
        for node in nodes:
            node_name = node['node']
            if check_node_permission(permissions, node_name, has_dc_permission):
                allowed_nodes.append(node_name)
            else:
                excluded_nodes.append(node_name)
        
        # Report results
        if allowed_nodes:
            logging.info(f"Nodes with Sys.Audit permission: {', '.join(allowed_nodes)}")
            if excluded_nodes:
                logging.warning(f"Nodes excluded from monitoring (no Sys.Audit permission): {', '.join(excluded_nodes)}")
                logging.warning("To monitor these nodes, assign 'Sys.Audit' role at /nodes/{node_name}")
            return allowed_nodes, None
        else:
            # No nodes have permission
            error_msg = "Sys.Audit permission not found for any node"
            logging.error(f"Permission check failed: {error_msg}")
            logging.error(f"Checked {len(nodes)} node(s): {', '.join([n['node'] for n in nodes])}")
            logging.error("")
            logging.error("Required permissions for this application:")
            logging.error("  - Sys.Audit permission on the datacenter or node level")
            logging.error("    (This allows reading system audit logs and task information)")
            logging.error("")
            logging.error("If you're using an API token with 'Privilege Separation' enabled:")
            logging.error("  1. Go to Datacenter > Permissions > API Tokens")
            logging.error("  2. Edit your token")
            logging.error("  3. Assign the 'Sys.Audit' role to the token at:")
            logging.error("     - Datacenter level: / (gives access to all nodes)")
            logging.error("     - Or node level: /nodes/{node_name} (gives access to specific node)")
            logging.error("  4. Ensure the associated user also has the required permissions")
            logging.error("     (Token permissions are the intersection of user and token permissions)")
            logging.error("")
            logging.error("Alternatively, you can disable 'Privilege Separation' to grant")
            logging.error("the token all permissions that the associated user has.")
            return [], error_msg
            
    except ResourceException as e:
        error_msg = str(e)
        if "403" in error_msg or "Forbidden" in error_msg or "Permission check failed" in error_msg:
            logging.error(f"Permission check failed: Cannot access permissions endpoint: {error_msg}")
            logging.error("This indicates that the token lacks permissions to query its own permissions.")
            logging.error("")
            logging.error("If you're using an API token with 'Privilege Separation' enabled:")
            logging.error("  1. Go to Datacenter > Permissions > API Tokens")
            logging.error("  2. Edit your token")
            logging.error("  3. Assign the 'Sys.Audit' role to the token")
            logging.error("  4. Or disable 'Privilege Separation' to grant full user permissions")
            return [], error_msg
        else:
            # Other ResourceException - re-raise
            raise
    except Exception as e:
        # Re-raise other exceptions (connection issues, etc.)
        logging.error(f"Unexpected error checking permissions: {e}")
        raise

async def get_proxmox_tasks(proxmox, since, allowed_nodes=None):
    if allowed_nodes is None:
        # If no filter specified, get all nodes
        nodes = proxmox.nodes.get()
        allowed_nodes = [node['node'] for node in nodes]
    
    tasks = []
    for node_name in allowed_nodes:
        try:
            node_tasks = proxmox.nodes(node_name).tasks.get(since=since, source="all")
            tasks.extend(node_tasks)
        except ResourceException as e:
            error_msg = str(e)
            if "403" in error_msg or "Forbidden" in error_msg:
                logging.warning(f"Lost permission to access tasks on node {node_name}: {error_msg}")
            else:
                # Re-raise other ResourceExceptions
                raise
        except Exception as e:
            logging.error(f"Error fetching tasks from node {node_name}: {e}")
            # Continue with other nodes
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

async def fetch_tasks(proxmox, allowed_nodes=None):
    logging.info(f'Fetching tasks from {len(allowed_nodes) if allowed_nodes else "all"} node(s)...')
    current_time = int(time.time())

    while True:
        try:
            tasks = await get_proxmox_tasks(proxmox, current_time, allowed_nodes)

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
        task = await queue.get()
        task_id = task['upid']
        logging.info(f"Processing {task_id} from queue...")

        if not task_handlers.get(task_id):
            task_handler = asyncio.create_task(monitor_task(proxmox, task))
            task_handler.set_name(task_id)
            task_handlers[task_id] = task_handler
            logging.info(f"Started handler for task {task_id}")

async def monitor(proxmox_host=None, proxmox_port=None, proxmox_user=None, 
                  proxmox_pass=None, proxmox_token_name=None, proxmox_token_value=None,
                  verify_ssl=False):

    logging.info(f"Monitoring {proxmox_host}:{proxmox_port}...")
    
    # Determine authentication method
    use_token = bool(proxmox_token_name and proxmox_token_value)
    logging.info(f"Using token authentication: {use_token}")

    proxmox = None

    try:
        if use_token:
            # API token authentication
            # According to proxmoxer docs: https://proxmoxer.github.io/docs/latest/authentication/
            logging.info(f"Using Proxmox API token authentication: user={proxmox_user}, token_name={proxmox_token_name}")
            proxmox = proxmoxer.ProxmoxAPI(proxmox_host,
                                            port=proxmox_port,
                                            user=proxmox_user,
                                            token_name=proxmox_token_name,
                                            token_value=proxmox_token_value,
                                            verify_ssl=verify_ssl)
        else:
            # Standard username/password authentication
            logging.info(f"Using Proxmox password authentication: {proxmox_user}")
            proxmox = proxmoxer.ProxmoxAPI(proxmox_host,
                                            port=proxmox_port,
                                            user=proxmox_user,
                                            password=proxmox_pass,
                                            verify_ssl=verify_ssl)
        
        # Test connection by trying to get nodes and validate response
        nodes = proxmox.nodes.get()
        if not isinstance(nodes, list):
            raise ValueError(f"Invalid response from Proxmox API: expected list of nodes, got {type(nodes)}")
        logging.info(f"Successfully connected to Proxmox API at {proxmox_host}:{proxmox_port} (found {len(nodes)} node(s))")
        
        # Check permissions for accessing tasks
        allowed_nodes = None  # None means all nodes (for password auth or full permissions)
        
        if use_token:
            logging.info("Checking API token permissions...")
            allowed_nodes, perm_error = await check_permissions(proxmox, nodes)
            if not allowed_nodes:
                raise PermissionError(f"API token lacks required permissions to access tasks: {perm_error}")
            logging.info(f"API token permissions verified: will monitor {len(allowed_nodes)} node(s)")
        else:
            # For password auth, we assume full permissions (user's own permissions)
            logging.debug("Password authentication: skipping explicit permission check, monitoring all nodes")
            allowed_nodes = None  # Monitor all nodes
        
    except PermissionError:
        # Re-raise permission errors as-is (they already have helpful messages)
        raise
    except Exception as e:
        error_msg = str(e)
        # Check if it's a connection-related error
        is_connection_error = any(err in error_msg.lower() for err in [
            'connection refused', 'connection error', 'timeout', 
            'name resolution', 'failed to resolve'
        ])
        
        logging.error(f"Failed to connect to Proxmox API: {error_msg}")
        
        if is_connection_error:
            logging.error(f"Connection is being refused by {proxmox_host}:{proxmox_port}")
            logging.error(f"Please check:")
            logging.error(f"  1. Firewall rules: Is the Proxmox server allowing connections from this container?")
            logging.error(f"  2. Service status: Is the Proxmox API service running on port {proxmox_port}?")
            logging.error(f"  3. Network access: Can the container reach {proxmox_host}:{proxmox_port}?")
            logging.error(f"  4. Authentication: Verify your credentials are correct")
        else:
            logging.error(f"This appears to be an authentication or API error, not a network issue")
            logging.error(f"Please verify your credentials and token permissions")
        
        raise ConnectionError(f"Unable to connect to Proxmox API at {proxmox_host}:{proxmox_port}: {error_msg}")

    fetch_task = asyncio.create_task(fetch_tasks(proxmox, allowed_nodes))
    process_task = asyncio.create_task(process_tasks(proxmox))

    await fetch_task
    await process_task

if __name__ == "__main__":
    # Initialize logging first so we can log configuration and errors
    log_level = os.getenv('LOG_LEVEL', "INFO")
    logging.basicConfig(
        format='%(asctime)s %(levelname)s %(message)s',
        level=log_level,
        stream=sys.stdout)
    
    # Validate Ntfy server configuration
    if not NTFY_SERVER_URL:
        logging.error("Mandatory environment variable NTFY_SERVER_URL is not set")
        sys.exit(1)
    
    # Validate Proxmox configuration
    proxmox_api_url = os.getenv('PROXMOX_API_URL', None)
    if not proxmox_api_url:
        logging.error("Mandatory environment variable PROXMOX_API_URL is not set")
        sys.exit(1)

    proxmox_port = os.getenv('PROXMOX_PORT', None)
    if not proxmox_port:
        logging.error("Mandatory environment variable PROXMOX_PORT is not set")
        sys.exit(1)
    try:
        proxmox_port = int(proxmox_port)
    except ValueError:
        logging.error(f"Invalid PROXMOX_PORT value: {proxmox_port}, must be an integer")
        sys.exit(1)

    verify_ssl = os.getenv('VERIFY_SSL', 'False')
    verify_ssl = verify_ssl.lower() in ('true', '1', 'yes', 'on')
    logging.info(f"VERIFY_SSL: {verify_ssl}")

    proxmox_user = os.getenv('PROXMOX_USER', None)
    if not proxmox_user:
        logging.error("Mandatory environment variable PROXMOX_USER is not set")
        sys.exit(1)

    proxmox_pass = os.getenv('PROXMOX_PASS', None)
    proxmox_token_name = os.getenv('PROXMOX_TOKEN_NAME', None)
    proxmox_token_value = os.getenv('PROXMOX_TOKEN_VALUE', None)
    if not proxmox_pass and not proxmox_token_name and not proxmox_token_value:
        logging.error("Mandatory environment variable PROXMOX_PASS or PROXMOX_TOKEN_NAME or PROXMOX_TOKEN_VALUE is not set")
        logging.error("For password authentication, set PROXMOX_PASS")
        logging.error("For token authentication, set PROXMOX_TOKEN_NAME and PROXMOX_TOKEN_VALUE")
        sys.exit(1)
    
    logging.info(f"Proxmox configuration: proxmox_api_url={proxmox_api_url}, proxmox_port={proxmox_port}, proxmox_user={proxmox_user}, proxmox_token_name={proxmox_token_name}")

    try:
        asyncio.run(monitor(proxmox_api_url, proxmox_port, proxmox_user, proxmox_pass,
                           proxmox_token_name, proxmox_token_value, verify_ssl))
    except PermissionError as e:
        # Permission errors are already logged with helpful messages
        logging.error("Exiting due to insufficient permissions")
        sys.exit(1)
    except ConnectionError as e:
        # Connection errors are already logged with helpful messages
        logging.error("Exiting due to connection error")
        sys.exit(1)
    except KeyboardInterrupt:
        logging.info("Received interrupt signal, shutting down gracefully")
        sys.exit(0)
    except Exception as e:
        # Unexpected errors should still show traceback for debugging
        logging.error(f"Unexpected error: {e}")
        raise
