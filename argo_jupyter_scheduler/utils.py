import json
import logging
import os
import re
import shlex
from enum import Enum
from pathlib import Path
from urllib.parse import urljoin

import urllib3
from hera.shared import global_config
from jupyter_scheduler.utils import create_output_filename
from urllib3.exceptions import ConnectionError

CONDA_STORE_TOKEN = "CONDA_STORE_TOKEN"
CONDA_STORE_SERVICE = "CONDA_STORE_SERVICE"
CONDA_STORE_SERVICE_NAMESPACE = "CONDA_STORE_SERVICE_NAMESPACE"

CONDA_ENV_LOCATION = "/opt/conda/envs/{conda_env_name}"
CONDA_STORE_ENV_LOCATION = "/home/conda/{env_namespace}/envs/{conda_env_name}"
KUBERNETES_SERVICE_ACCOUNT_TOKEN = "/var/run/secrets/kubernetes.io/serviceaccount/token"
DEFAULT_ENV = "default"


class WorkflowActionsEnum(Enum):
    create = "create"
    update = "update"
    delete = "delete"
    stop = "stop"


def setup_logger(name):
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)

    handler = logging.StreamHandler()
    handler.setLevel(logging.INFO)

    formatter = logging.Formatter("{name:}:{levelname:}: {message}", style="{")
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    return logger


def add_file_logger(logger, log_path):
    logger.setLevel(logging.DEBUG)
    fh = logging.FileHandler(log_path)
    logger.addHandler(fh)


logger = setup_logger(__name__)


def authenticate():
    namespace = os.environ.get("ARGO_NAMESPACE") or os.environ.get(
        "POD_NAMESPACE", "dev"
    )

    token = os.environ.get("ARGO_TOKEN")
    if not token and Path(KUBERNETES_SERVICE_ACCOUNT_TOKEN).is_file():
        token = Path(KUBERNETES_SERVICE_ACCOUNT_TOKEN).read_text().strip()
    if not token:
        msg = "ARGO_TOKEN"
        raise KeyError(msg)
    if token.startswith("Bearer"):
        token = token.split(" ")[-1]

    base_href = os.environ.get("ARGO_BASE_HREF", "/")
    if not base_href.endswith("/"):
        base_href += "/"

    server = os.environ["ARGO_SERVER"]
    if "://" not in server:
        scheme = os.environ.get("ARGO_SERVER_SCHEME", "https")
        server = f"{scheme}://{server}"
    host = urljoin(server, base_href)

    global_config.host = host
    global_config.token = token
    global_config.verify_ssl = os.environ.get(
        "ARGO_VERIFY_SSL", "true"
    ).lower() not in {"0", "false", "no"}
    global_config.namespace = namespace

    return global_config


def gen_workflow_name(job_id: str):
    return f"job-{job_id}"


def gen_cron_workflow_name(job_definition_id: str, job_definition_name: str = ""):
    name = re.sub(r"[^a-z0-9-]+", "-", job_definition_name.lower()).strip("-")
    suffix = job_definition_id[:13]
    prefix = f"job-def-{name}-" if name else "job-def-"
    if name:
        max_name_length = 63 - len("job-def-") - len(suffix) - 2
        name = name[:max_name_length].rstrip("-")
        prefix = f"job-def-{name}-" if name else "job-def-"
    return f"{prefix}{suffix}"


def gen_default_output_path(input_path: str):
    # The initial filename before we can get access to the timestamp. Has the
    # "0" suffix to avoid clashing with the input filename. This value will be
    # pretty-printed as the Unix epoch when the file is created.
    return gen_output_path(input_path, 0)


def gen_default_html_path(input_path: str):
    # The initial filename before we can get access to the timestamp. Has the
    # "0" suffix to avoid clashing with the input filename. This value will be
    # pretty-printed as the Unix epoch when the file is created.
    return gen_html_path(input_path, 0)


def gen_output_path(input_path: str, start_time: int):
    # It's important to use this exact format to make files downloadable via the
    # web UI.
    return create_output_filename(input_path, start_time, "ipynb")


def gen_html_path(input_path: str, start_time: int):
    # It's important to use this exact format to make files downloadable via the
    # web UI.
    return create_output_filename(input_path, start_time, "html")


def gen_log_path(input_path: str):
    p = Path(input_path)
    return str(p.parent / "logs.txt")


def gen_papermill_status_path(input_path: str):
    p = Path(input_path)
    return str(p.parent / "papermill_status.txt")


def gen_pod_spec_patch(
    image: str = "",
    service_account_name: str = "",
    pvc_name: str = "",
    pvc_mount_path: str = "/home/jovyan",
):
    """Build an optional pod patch for non-Nebari workflow deployments."""
    patch = {}

    if service_account_name:
        patch["serviceAccountName"] = service_account_name

    if image or pvc_name:
        container = {"name": "main"}
        if image:
            container["image"] = image
        if pvc_name:
            container["volumeMounts"] = [
                {"name": "jupyter-user-home", "mountPath": pvc_mount_path}
            ]
        patch["containers"] = [container]

    if pvc_name:
        patch["volumes"] = [
            {
                "name": "jupyter-user-home",
                "persistentVolumeClaim": {"claimName": pvc_name},
            }
        ]

    return json.dumps(patch) if patch else None


def send_request(api_v1_endpoint):
    token = os.environ[CONDA_STORE_TOKEN]
    conda_store_svc_name = os.environ[CONDA_STORE_SERVICE]
    conda_store_svc_namespace = os.environ[CONDA_STORE_SERVICE_NAMESPACE]

    conda_store_svc_name, conda_store_service_port = conda_store_svc_name.split(":")
    conda_store_endpoint = f"http://{conda_store_svc_name}.{conda_store_svc_namespace}.svc:{conda_store_service_port}/conda-store/api/v1/"
    url = urljoin(conda_store_endpoint, api_v1_endpoint)

    http = urllib3.PoolManager()
    response = http.request("GET", url, headers={"Authorization": f"Bearer {token}"})

    j = json.loads(response.data.decode("UTF-8"))

    try:
        return j["data"]
    except KeyError as e:
        raise ConnectionError from e


def gen_conda_env_path(conda_env_name: str, use_conda_store_env: bool = True):
    # TODO: validate that `papermill` is in the specified conda environment

    if use_conda_store_env:
        try:
            conda_store_envs = send_request("environment")

            available_ns = []
            available_env_names = []
            available_envs = []
            for i in conda_store_envs:
                available_ns.append(i["namespace"]["name"])
                available_env_names.append(i["name"])
                available_envs.append(f'{i["namespace"]["name"]}-{i["name"]}')

            def _valid_env(
                s,
                available_ns=available_ns,
                available_env_names=available_env_names,
                available_envs=available_envs,
            ):
                parts = s.split("-")
                for i in range(1, len(parts)):
                    namespace = "-".join(parts[:i])
                    name = "-".join(parts[i:])
                    if (
                        namespace in available_ns
                        and name in available_env_names
                        and s in available_envs
                    ):
                        return namespace, name
                return False

            env_namespace_name = _valid_env(conda_env_name)
            if env_namespace_name:
                env_namespace, _ = env_namespace_name
                return CONDA_STORE_ENV_LOCATION.format(
                    env_namespace=env_namespace, conda_env_name=conda_env_name
                )

        except ConnectionError as e:
            logger.info(f"Unable to connect to conda-store. Encountered error:\n{e}")

    else:
        conda_env_path = Path(CONDA_ENV_LOCATION.format(conda_env_name=conda_env_name))
        if conda_env_path.exists():
            return conda_env_path

    logger.info(
        f"Conda environment `{conda_env_name}` not found, falling back to `{DEFAULT_ENV}`."
    )
    return CONDA_ENV_LOCATION.format(conda_env_name=DEFAULT_ENV)


def gen_papermill_command_input(
    conda_env_name: str,
    input_path: str,
    output_path: str,
    html_path: str,
    log_path: str,
    papermill_status_path: str,
    use_conda_store_env: bool = True,
    use_conda_env: bool = True,
):
    # TODO: allow overrides
    kernel_name = "python3"

    conda_env_path = None
    if use_conda_env:
        conda_env_path = gen_conda_env_path(conda_env_name, use_conda_store_env)
        logger.info(f"conda_env_path: {conda_env_path}")
    logger.info(f"output_path: {output_path}")
    logger.info(f"log_path: {log_path}")
    logger.info(f"html_path: {html_path}")
    logger.info(f"papermill_status_path: {papermill_status_path}")

    if not use_conda_env:
        quote = shlex.quote
        papermill = (
            f"( papermill -k {quote(kernel_name)} {quote(input_path)} "
            f"{quote(output_path)} ; ec=$? ; echo $ec > "
            f"{quote(papermill_status_path)} ; exit $ec )"
        )
        jupyter = (
            f"jupyter nbconvert --to html {quote(output_path)} "
            f"--output {quote(html_path)}"
        )
        redirection = f">> {quote(log_path)} 2>&1"
        return f"{{ {papermill} && {jupyter} ; }} {redirection}"

    # Within a single-quoted string, wraps strings in shell-safe quotes.
    def sq(s):
        return rf"'\''{s}'\''"

    # These commands are executed within a single-quoted string below
    papermill = (
        f"( papermill -k {sq(kernel_name)} {sq(input_path)} {sq(output_path)} ; "
        f"ec=$? ; echo $ec > {sq(papermill_status_path)} ; exit $ec )"
    )
    jupyter = f"jupyter nbconvert --to html {sq(output_path)} --output {sq(html_path)}"

    # It's important that inner quotes are single quotes to prevent shell expansion
    command = f"{{ {papermill} && {jupyter} ; }}"
    return (
        f"conda run -p '{conda_env_path}' /bin/sh -c '{command} >> {sq(log_path)} 2>&1'"
    )


def sanitize_label(s: str):
    s = s.lower()
    pattern = r"[^A-Za-z0-9]"
    return re.sub(pattern, lambda x: "-" + hex(ord(x.group()))[2:], s)


def desanitize_label(s: str):
    pattern = r"-([A-Za-z0-9][A-Za-z0-9])"
    return re.sub(pattern, lambda x: chr(int(x.group(1), 16)), s)
