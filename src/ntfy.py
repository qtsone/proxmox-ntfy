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
from typing import Optional, List, Dict, Tuple, Set, Any

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Ntfy server details (validation moved to __main__ after logging setup)
NTFY_SERVER_URL = os.getenv('NTFY_SERVER_URL', None)
NTFY_TOKEN = os.getenv('NTFY_TOKEN', None)
NTFY_USER = os.getenv('NTFY_USER', None)
NTFY_PASS = os.getenv('NTFY_PASS', None)

# Constants
SYS_AUDIT_PERMISSION = "Sys.Audit"
DEFAULT_TASK_TIMEOUT = 1800

# Error messages
ERROR_MESSAGES = {
    'insufficient_permissions': """Required permissions for this application:
  - Sys.Audit permission on the datacenter or node level
    (This allows reading system audit logs and task information)

If you're using an API token with 'Privilege Separation' enabled:
  1. Go to Datacenter > Permissions > API Tokens
  2. Edit your token
  3. Assign the 'Sys.Audit' role to the token at:
     - Datacenter level: / (gives access to all nodes)
     - Or node level: /nodes/{node_name} (gives access to specific node)
  4. Ensure the associated user also has the required permissions
     (Token permissions are the intersection of user and token permissions)

Alternatively, you can disable 'Privilege Separation' to grant
the token all permissions that the associated user has.""",
    
    'permission_check_failed': """This indicates that the token lacks permissions to query its own permissions.

If you're using an API token with 'Privilege Separation' enabled:
  1. Go to Datacenter > Permissions > API Tokens
  2. Edit your token
  3. Assign the 'Sys.Audit' role to the token
  4. Or disable 'Privilege Separation' to grant full user permissions""",
    
    'connection_failed': """Please check:
  1. Firewall rules: Is the Proxmox server allowing connections from this container?
  2. Service status: Is the Proxmox API service running on port {port}?
  3. Network access: Can the container reach {host}:{port}?
  4. Authentication: Verify your credentials are correct"""
}


def _log_error_message(message_key: str, **kwargs) -> None:
    """Log a multi-line error message from ERROR_MESSAGES.
    
    Args:
        message_key: Key in ERROR_MESSAGES dictionary
        **kwargs: Format arguments for the message (e.g., port, host)
    """
    message = ERROR_MESSAGES.get(message_key, f"Unknown error message key: {message_key}")
    if kwargs:
        message = message.format(**kwargs)
    for line in message.strip().split('\n'):
        logging.error(line)


task_handlers = {}
queue = asyncio.Queue()
processed_tasks = set()


async def send_notification(title: str, tags: str, message: str) -> None:
    """Send a notification to the Ntfy server.
    
    Args:
        title: Notification title
        tags: Comma-separated tags for the notification
        message: Notification message body (Markdown supported)
    """
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

async def check_permissions(proxmox: proxmoxer.ProxmoxAPI, nodes: List[Dict[str, Any]]) -> None:
    """Verify that the API token has permission to access tasks.
    
    Uses a simple approach: try to fetch tasks from the first available node.
    If this fails with a 403 error, the token lacks required permissions.
    Node-level permission filtering is handled by get_proxmox_tasks().
    
    Args:
        proxmox: Authenticated ProxmoxAPI instance
        nodes: List of node dictionaries from proxmox.nodes.get()
        
    Raises:
        PermissionError: If API token lacks required permissions to access tasks
        ResourceException: For non-403 API errors
    """
    if not nodes:
        raise PermissionError("No nodes found to check permissions")
    
    # Try fetching tasks from the first node to verify we have permissions
    # This is simpler than parsing the permissions API response
    first_node = nodes[0]['node']
    try:
        # Try to fetch recent tasks (since=0 means all tasks)
        proxmox.nodes(first_node).tasks.get(since=0, source="all")
        logging.info(f"Permission check passed: API token can access tasks on node {first_node}")
    except ResourceException as e:
        error_msg = str(e)
        # Check status code if available, otherwise fall back to string matching
        is_permission_error = False
        if hasattr(e, 'status_code'):
            is_permission_error = e.status_code == 403
        else:
            # Fallback for older proxmoxer versions
            is_permission_error = "403" in error_msg or "Forbidden" in error_msg
        
        if is_permission_error:
            logging.error(f"Permission check failed: Cannot access tasks on node {first_node}: {error_msg}")
            logging.error("")
            _log_error_message('insufficient_permissions')
            raise PermissionError(f"API token lacks required {SYS_AUDIT_PERMISSION} permission to access tasks")
        else:
            # Other ResourceException - re-raise
            raise

async def get_proxmox_tasks(proxmox: proxmoxer.ProxmoxAPI, since: int, allowed_nodes: List[str]) -> List[Dict[str, Any]]:
    """Fetch Proxmox tasks from specified nodes.
    
    Args:
        proxmox: Authenticated ProxmoxAPI instance
        since: Unix timestamp to fetch tasks since
        allowed_nodes: List of node names to fetch from (required)
        
    Returns:
        List of task dictionaries from Proxmox API
        
    Raises:
        ResourceException: For API errors (except 403 permission errors which are logged)
    """
    tasks = []
    for node_name in allowed_nodes:
        try:
            node_tasks = proxmox.nodes(node_name).tasks.get(since=since, source="all")
            tasks.extend(node_tasks)
        except ResourceException as e:
            error_msg = str(e)
            # Check status code if available, otherwise fall back to string matching
            is_permission_error = False
            if hasattr(e, 'status_code'):
                is_permission_error = e.status_code == 403
            else:
                # Fallback for older proxmoxer versions
                is_permission_error = "403" in error_msg or "Forbidden" in error_msg
            
            if is_permission_error:
                logging.warning(f"Lost permission to access tasks on node {node_name}: {error_msg}")
            else:
                # Re-raise other ResourceExceptions
                raise
        except Exception as e:
            logging.error(f"Error fetching tasks from node {node_name}: {e}")
            # Continue with other nodes
    return tasks

async def get_task_status(proxmox: proxmoxer.ProxmoxAPI, node: str, task_id: str) -> Dict[str, Any]:
    """Get the status of a Proxmox task.
    
    Args:
        proxmox: Authenticated ProxmoxAPI instance
        node: Node name where the task is running
        task_id: Task UPID
        
    Returns:
        Dictionary containing task status information
    """
    status = proxmox.nodes(node).tasks(task_id).status.get()
    logging.debug(f"STATUS [{task_id}] {status}")
    return status

async def get_task_log(proxmox: proxmoxer.ProxmoxAPI, node: str, task_id: str) -> List[str]:
    """Get the log entries for a Proxmox task.
    
    Args:
        proxmox: Authenticated ProxmoxAPI instance
        node: Node name where the task is running
        task_id: Task UPID
        
    Returns:
        List of log entry text strings
    """
    log = proxmox.nodes(node).tasks(task_id).log.get()
    logging.debug(f"LOG [{task_id}] {log}")
    return [log_entry['t'] for log_entry in log if log_entry['t']]

async def monitor_task(proxmox: proxmoxer.ProxmoxAPI, task: Dict[str, Any]) -> str:
    """Monitor a Proxmox task until completion and send notification.
    
    Args:
        proxmox: Authenticated ProxmoxAPI instance
        task: Task dictionary from Proxmox API
        
    Returns:
        Task UPID that was monitored
    """
    task_id = task['upid']
    _, node, uuid, _ = task_id.split(":", maxsplit=3)
    logging.info(f"[{uuid}] Task found. Monitoring...")
    start_time = time.time()
    timeout = int(os.getenv('TASK_TIMEOUT', DEFAULT_TASK_TIMEOUT))
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
    message = (
        f"## Task Details\n\n"
        f"**Status**: {exitstatus}\n"
        f"**User**: {task['user']}\n\n"
        f"### Task Status\n"
        f"```json\n"
        f"{json.dumps(task_status, indent=2)}\n"
        f"```\n\n"
        f"### Task Log\n"
        f"```json\n"
        f"{json.dumps(log_entries, indent=2)}\n"
        f"```\n"
    )

    await send_notification(title, tags, message)
    logging.info(f"Task {task_id} processed.")
    return task_id

async def fetch_tasks(proxmox: proxmoxer.ProxmoxAPI, allowed_nodes: Optional[List[str]]) -> None:
    """Continuously fetch new Proxmox tasks and add them to the queue.
    
    Args:
        proxmox: Authenticated ProxmoxAPI instance
        allowed_nodes: Optional list of node names to monitor. If None, monitors all nodes.
    """
    if allowed_nodes is None:
        # Get all nodes if not specified
        nodes = proxmox.nodes.get()
        allowed_nodes = [node['node'] for node in nodes]
    logging.info(f'Fetching tasks from {len(allowed_nodes)} node(s)...')
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

async def process_tasks(proxmox: proxmoxer.ProxmoxAPI) -> None:
    """Continually process tasks from the queue.
    
    Creates monitoring tasks for each queued task and tracks them.
    
    Args:
        proxmox: Authenticated ProxmoxAPI instance
    """
    while True:
        task = await queue.get()
        task_id = task['upid']
        logging.info(f"Processing {task_id} from queue...")

        if not task_handlers.get(task_id):
            task_handler = asyncio.create_task(monitor_task(proxmox, task))
            task_handler.set_name(task_id)
            task_handlers[task_id] = task_handler
            logging.info(f"Started handler for task {task_id}")


def create_proxmox_client(proxmox_host: str, proxmox_port: int, proxmox_user: str,
                         proxmox_pass: Optional[str] = None, proxmox_token_name: Optional[str] = None,
                         proxmox_token_value: Optional[str] = None, verify_ssl: bool = False) -> proxmoxer.ProxmoxAPI:
    """Create and return authenticated Proxmox API client.
    
    Args:
        proxmox_host: Proxmox server hostname or IP
        proxmox_port: Proxmox API port
        proxmox_user: Proxmox username
        proxmox_pass: Proxmox password (for password authentication)
        proxmox_token_name: API token name (for token authentication)
        proxmox_token_value: API token value (for token authentication)
        verify_ssl: Whether to verify SSL certificates
        
    Returns:
        Authenticated ProxmoxAPI instance
    """
    use_token = bool(proxmox_token_name and proxmox_token_value)
    
    if use_token:
        logging.info(f"Using Proxmox API token authentication: user={proxmox_user}, token_name={proxmox_token_name}")
        return proxmoxer.ProxmoxAPI(proxmox_host,
                                    port=proxmox_port,
                                    user=proxmox_user,
                                    token_name=proxmox_token_name,
                                    token_value=proxmox_token_value,
                                    verify_ssl=verify_ssl)
    else:
        logging.info(f"Using Proxmox password authentication: {proxmox_user}")
        return proxmoxer.ProxmoxAPI(proxmox_host,
                                    port=proxmox_port,
                                    user=proxmox_user,
                                    password=proxmox_pass,
                                    verify_ssl=verify_ssl)


def validate_connection(proxmox: proxmoxer.ProxmoxAPI, proxmox_host: str, proxmox_port: int) -> List[Dict[str, Any]]:
    """Test connection and return list of nodes.
    
    Args:
        proxmox: Authenticated ProxmoxAPI instance
        proxmox_host: Proxmox server hostname (for logging)
        proxmox_port: Proxmox API port (for logging)
        
    Returns:
        List of node dictionaries from Proxmox API
        
    Raises:
        ValueError: If API returns invalid response format
    """
    nodes = proxmox.nodes.get()
    if not isinstance(nodes, list):
        raise ValueError(f"Invalid response from Proxmox API: expected list of nodes, got {type(nodes)}")
    logging.info(f"Successfully connected to Proxmox API at {proxmox_host}:{proxmox_port} (found {len(nodes)} node(s))")
    return nodes


async def get_allowed_nodes(proxmox: proxmoxer.ProxmoxAPI, nodes: List[Dict[str, Any]], 
                           use_token: bool) -> Optional[List[str]]:
    """Verify permissions and determine which nodes can be monitored.
    
    For token authentication, verifies that the token has permission to access tasks.
    Node-level permission filtering is handled automatically by get_proxmox_tasks()
    which will skip nodes without permission.
    
    Args:
        proxmox: Authenticated ProxmoxAPI instance
        nodes: List of node dictionaries from proxmox.nodes.get()
        use_token: Whether token authentication is being used
        
    Returns:
        None to monitor all nodes (node-level filtering happens during task fetching)
        
    Raises:
        PermissionError: If API token lacks required permissions
    """
    if use_token:
        logging.info("Checking API token permissions...")
        await check_permissions(proxmox, nodes)
        logging.info("API token permissions verified: will monitor all accessible nodes")
        # Return None to monitor all nodes - get_proxmox_tasks() will handle node-level filtering
        return None
    else:
        # For password auth, we assume full permissions (user's own permissions)
        logging.debug("Password authentication: skipping explicit permission check, monitoring all nodes")
        return None  # Monitor all nodes


async def monitor(proxmox_host: Optional[str] = None, proxmox_port: Optional[int] = None, 
                  proxmox_user: Optional[str] = None, proxmox_pass: Optional[str] = None, 
                  proxmox_token_name: Optional[str] = None, proxmox_token_value: Optional[str] = None,
                  verify_ssl: bool = False) -> None:
    """Main monitoring function that sets up Proxmox connection and starts monitoring tasks.
    
    Creates authenticated Proxmox API client, validates connection, checks permissions,
    and starts background tasks for fetching and processing Proxmox tasks.
    
    Args:
        proxmox_host: Proxmox server hostname or IP
        proxmox_port: Proxmox API port
        proxmox_user: Proxmox username
        proxmox_pass: Proxmox password (for password authentication)
        proxmox_token_name: API token name (for token authentication)
        proxmox_token_value: API token value (for token authentication)
        verify_ssl: Whether to verify SSL certificates
        
    Raises:
        PermissionError: If API token lacks required permissions
        ConnectionError: If unable to connect to Proxmox API
        ValueError: If API returns invalid response format
    """

    logging.info(f"Monitoring {proxmox_host}:{proxmox_port}...")
    
    # Determine authentication method
    use_token = bool(proxmox_token_name and proxmox_token_value)
    logging.info(f"Using token authentication: {use_token}")

    try:
        # Create authenticated Proxmox client
        proxmox = create_proxmox_client(proxmox_host, proxmox_port, proxmox_user,
                                       proxmox_pass, proxmox_token_name, proxmox_token_value,
                                       verify_ssl)
        
        # Validate connection and get nodes
        nodes = validate_connection(proxmox, proxmox_host, proxmox_port)
        
        # Determine which nodes can be monitored
        allowed_nodes = await get_allowed_nodes(proxmox, nodes, use_token)
        
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
            _log_error_message('connection_failed', host=proxmox_host, port=proxmox_port)
        else:
            logging.error(f"This appears to be an authentication or API error, not a network issue")
            logging.error(f"Please verify your credentials and token permissions")
        
        raise ConnectionError(f"Unable to connect to Proxmox API at {proxmox_host}:{proxmox_port}: {error_msg}")

    # Start background monitoring tasks
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
