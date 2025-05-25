import os
import re
import json
import glob
import time
import shutil
import signal
import random
import asyncio
import logging
import argparse
import subprocess
from pathlib import Path
from phrase import WORD_LIST
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import RotatingFileHandler

LOG_FILE = 'bot_manager.log'
LOG_SIZE = 10 * 1024 * 1024  # 10 MB
LOG_BACKUP_COUNT = 5
SUPERVISORD_CONF_DIR = "/etc/supervisor/conf.d"

file_handler = RotatingFileHandler(LOG_FILE, maxBytes=LOG_SIZE, backupCount=LOG_BACKUP_COUNT)
console_handler = logging.StreamHandler()
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
file_handler.setFormatter(formatter)
console_handler.setFormatter(formatter)
file_handler.setLevel(logging.DEBUG)
console_handler.setLevel(logging.DEBUG)
logging.getLogger().handlers.clear()
logging.getLogger().addHandler(file_handler)
logging.getLogger().addHandler(console_handler)
logging.getLogger().setLevel(logging.DEBUG)

def generate_prefix():
    word1 = random.choice(WORD_LIST)
    word2 = random.choice(WORD_LIST)
    prefix = f"{word1} {word2}"
    logging.info(f'Generated prefix: {prefix}')
    return prefix

def get_pyenv_python(version):
    major_minor = '.'.join(version.split('.')[:2])
    shim = shutil.which(f"python{major_minor}")
    if shim:
        return shim
    logging.warning(f"pyenv shim python{major_minor} not found in PATH. Falling back to 'python3'.")
    return shutil.which("python3") or "python3"

def run_with_pyenv(version, command_args, **kwargs):
    env = os.environ.copy()
    env["PYENV_VERSION"] = version
    kwargs["env"] = env
    return subprocess.run(command_args, **kwargs)
    
def validate_config(clusters):
    required_keys = ['bot_number', 'git_url', 'branch', 'run_command']
    seen_bot_suffixes = set()
    bot_suffix_pattern = re.compile(r'bot\d+$')

    for cluster in clusters:
        if not all(key in cluster for key in required_keys):
            logging.error(f"Missing required fields in: {cluster.get('name', 'Unknown')}")
            return False

        if not cluster['git_url'].startswith('http'):
            logging.error(f"Invalid git_url for {cluster['name']}.")
            return False

        match = bot_suffix_pattern.search(cluster['bot_number'])
        if not match:
            logging.error(f"Invalid bot_number format for {cluster['name']}: {cluster['bot_number']}")
            return False

        bot_suffix = match.group()
        if bot_suffix in seen_bot_suffixes:
            logging.error(f"Duplicate bot suffix found: {bot_suffix} in {cluster['bot_number']}")
            return False

        seen_bot_suffixes.add(bot_suffix)

    logging.info("Configuration validation successful.")
    return True
    
async def cleanup_logs(log_dir='/var/log/supervisor', log_pattern='*_out.log', err_pattern='*_err.log', interval_hours=24):
    while True:
        try:
            for pattern in [log_pattern, err_pattern]:
                for log_file in glob.glob(os.path.join(log_dir, pattern)):
                    try:
                        os.remove(log_file)
                        logging.info(f"Deleted log file: {log_file}")
                    except Exception as e:
                        logging.error(f"Failed to delete log file {log_file}: {e}")
        except Exception as e:
            logging.error(f"Error during log cleanup: {e}")
        await asyncio.sleep(interval_hours * 3600)

def load_config(file_path):
    logging.info(f'Loading configuration from {file_path}')
    
    try:
        with open(file_path, "r") as jsonfile:
            config = json.load(jsonfile)
    except (json.JSONDecodeError, FileNotFoundError) as e:
        logging.error(f"Error loading JSON file: {e}")
        return []

    clusters = []
    for cluster in config.get('clusters', []):
        details_str = os.getenv(cluster['name'], '{}')
        
        try:
            details = json.loads(details_str)
            if not isinstance(details, list) or len(details) < 4:
                logging.warning(f"Skipping cluster {cluster['name']} due to missing details.")
                continue

            prefix = generate_prefix()
            cluster_name = f"{prefix} {cluster['name']}"
            cron_value = details[6] if len(details) > 6 else cluster.get("cron", None)

            clusters.append({
                "name": cluster_name,
                "bot_number": f"{prefix} {details[0]}",
                "git_url": details[1],
                "branch": details[2],
                "run_command": details[3],
                "env": details[4] if len(details) > 4 and isinstance(details[4], dict) else {},
                "python_version": details[5] if len(details) > 5 else None,
                "cron": cron_value
            })

        except json.JSONDecodeError:
            logging.error(f"Error decoding JSON for {cluster['name']}, skipping.")
            continue

    if not validate_config(clusters):
        raise ValueError("Invalid configuration file.")

    return clusters

load_dotenv('cluster.env', override=True)
clusters = load_config("config.json")

def write_supervisord_config(cluster, command):
    config_path = Path(SUPERVISORD_CONF_DIR) / f"{cluster['bot_number'].replace(' ', '_')}.conf"
    logging.info(f"Writing supervisord configuration for {cluster['bot_number']} at {config_path}")
    env_vars = ','.join([f'{key}="{value}"' for key, value in cluster['env'].items()]) if cluster['env'] else ""
    cron_line = f"cron={cluster['cron']}" if cluster.get('cron') else ""
    config_content = f"""
    [program:{cluster['bot_number'].replace(' ', '_')}]
    command={command}
    directory=/app/{cluster['bot_number'].replace(' ', '_')}
    autostart=true
    autorestart=true
    startretries=12
    stderr_logfile=/var/log/supervisor/{cluster['bot_number'].replace(' ', '_')}_err.log
    stdout_logfile=/var/log/supervisor/{cluster['bot_number'].replace(' ', '_')}_out.log
    {f"environment={env_vars}" if env_vars else ""}
    {cron_line}
    """
    config_path.write_text(config_content.strip())
    logging.info(f"Supervisord configuration for {cluster['bot_number']} written successfully.")

def _prepare_bot_dir(cluster):
    bot_dir = Path('/app') / cluster['bot_number'].replace(" ", "_")
    venv_dir = bot_dir / 'venv'
    requirements_file = bot_dir / 'requirements.txt'
    branch = cluster.get('branch', 'main')
    
    if bot_dir.exists():
        logging.info(f'Removing existing directory: {bot_dir}')
        shutil.rmtree(bot_dir)
    
    logging.info(f'Cloning {cluster["bot_number"]} from {cluster["git_url"]} (branch: {branch})')
    subprocess.run(['git', 'clone', '-b', branch, '--single-branch', cluster['git_url'], str(bot_dir)], check=True)
    version = cluster.get("python_version")
    if version:
        python_executable = get_pyenv_python(version)
    else:
        python_executable = shutil.which("python3") or "python3"
        
    if requirements_file.exists():
        logging.info(f'Creating virtual environment for {cluster["bot_number"]} using {python_executable}')
        if version:
            run_with_pyenv(version, [python_executable, '-m', 'venv', str(venv_dir)], check=True)
            pip_command = [str(venv_dir / 'bin' / 'pip'), 'install', '--no-cache-dir', '-r', str(requirements_file)]
            run_with_pyenv(version, pip_command, check=True)
        else:
             subprocess.run([python_executable, '-m', 'venv', str(venv_dir)], check=True)
             pip_command = [str(venv_dir / 'bin' / 'pip'), 'install', '--no-cache-dir', '-r', str(requirements_file)]
             subprocess.run(pip_command, check=True)
                
async def start_bot(cluster):
    logging.info(f'Starting bot: {cluster["bot_number"]}')
    bot_dir = Path('/app') / cluster['bot_number'].replace(" ", "_")
    venv_dir = bot_dir / 'venv'
    bot_file = bot_dir / cluster['run_command']
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _prepare_bot_dir, cluster)

    python_executable = venv_dir / 'bin' / 'python3'
    if cluster.get('python_version'):
        python_executable = venv_dir / 'bin' / f'python{cluster["python_version"]}'

    if bot_file.suffix == ".sh":
        command = f"bash {bot_file}"
    elif bot_file.suffix == ".py":
        command = f"{python_executable} {bot_file}"
    else:
        command = f"{python_executable} -m {bot_file.stem}"

    write_supervisord_config(cluster, command)

async def async_supervisorctl(command):
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        logging.error(f"Supervisorctl command failed: {stderr.decode()}")
    else:
        logging.info(f"Supervisorctl command succeeded: {stdout.decode()}")

async def reload_supervisord():
    logging.info("Reloading supervisord...")
    await async_supervisorctl("supervisorctl reread")
    await async_supervisorctl("supervisorctl update")
    logging.info("Supervisord updated successfully.")

async def get_process_status(bot_conf_name):
    proc = await asyncio.create_subprocess_shell(
        f"supervisorctl status {bot_conf_name}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode == 0:
        status_line = stdout.decode().strip()
        if status_line:
            parts = status_line.split()
            if len(parts) >= 2:
                return parts[1]
    return None

async def cleanup_existing_bots():
    conf_dir = Path(SUPERVISORD_CONF_DIR)
    for conf_file in conf_dir.glob("*.conf"):
        bot_conf_name = conf_file.stem
        await async_supervisorctl(f"supervisorctl stop {bot_conf_name}")
        conf_file.unlink()
        logging.info(f"Cleaned up supervisord config and stopped bot: {bot_conf_name}")
    await reload_supervisord()

async def wait_for_process_stop(bot_conf_name, timeout=30, interval=2):
    start_time = time.time()
    while time.time() - start_time < timeout:
        status = await get_process_status(bot_conf_name)
        if status != 'RUNNING':
            return True
        await asyncio.sleep(interval)
    return False

async def stop_bot(bot_number):
    logging.info(f"Stopping bot: {bot_number}")
    bot_conf_name = bot_number.replace(" ", "_")
    
    await async_supervisorctl(f"supervisorctl stop {bot_conf_name}")
    if not await wait_for_process_stop(bot_conf_name):
        logging.warning(f"Process {bot_conf_name} did not stop within timeout.")
    
    conf_path = Path(SUPERVISORD_CONF_DIR) / f"{bot_conf_name}.conf"
    if conf_path.exists():
        conf_path.unlink()
        logging.info(f"Removed supervisord configuration for {bot_number}.")

async def sort_bot_run_commands(clusters):
    tasks = [start_bot(cluster) for cluster in clusters]
    await asyncio.gather(*tasks)
    await reload_supervisord()

async def restart_all_bots():
    logging.info('Stopping all bots...')
    tasks = [stop_bot(cluster['bot_number']) for cluster in clusters]
    await asyncio.gather(*tasks)
    await reload_supervisord()

def signal_handler(sig, frame):
    logging.info('Shutting down...')
    asyncio.run(restart_all_bots())
    exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

async def main_async():
    parser = argparse.ArgumentParser(description='Bot Manager')
    parser.add_argument('--restart', action='store_true', help='Restart all bots')
    args = parser.parse_args()
    asyncio.create_task(cleanup_logs(interval_hours=168))
    await cleanup_existing_bots()

    if args.restart:
        logging.info('Restarting bot manager...')
        await asyncio.gather(*(async_supervisorctl(f"supervisorctl stop {cluster['bot_number'].replace(' ', '_')}") for cluster in clusters))
        await reload_supervisord()
    else:
        logging.info('Starting bot manager...')
        await sort_bot_run_commands(clusters)

if __name__ == "__main__":
    asyncio.run(main_async())
