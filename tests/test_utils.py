import json
from unittest.mock import Mock, patch

import pytest

from argo_jupyter_scheduler.executor import main_container
from argo_jupyter_scheduler.utils import (
    authenticate,
    gen_cron_workflow_name,
    gen_papermill_command_input,
    gen_pod_spec_patch,
    resolve_workflow_pvc_name,
    resolve_workflow_pod_affinity,
)


@pytest.mark.parametrize(
    "env, expected_namespace, expected_token, expected_host, exception",
    [
        # Test with missing env variables
        ({}, None, None, None, KeyError),
        # Test with valid env variables
        (
            {
                "ARGO_NAMESPACE": "test_namespace",
                "ARGO_TOKEN": "Bearer mytoken",
                "ARGO_BASE_HREF": "my_base_href",
                "ARGO_SERVER": "my_server",
            },
            "test_namespace",
            "mytoken",
            "https://my_server/my_base_href/",
            None,
        ),
        # Test with edge cases
        (
            {
                "ARGO_NAMESPACE": "",
                "ARGO_TOKEN": "mytoken",
                "ARGO_BASE_HREF": "my_base_href_without_slash",
                "ARGO_SERVER": "my_server",
            },
            "dev",
            "mytoken",
            "https://my_server/my_base_href_without_slash/",
            None,
        ),
        # Test without bearer in token
        (
            {
                "ARGO_NAMESPACE": "test_namespace",
                "ARGO_TOKEN": "mytoken_without_bearer",
                "ARGO_BASE_HREF": "my_base_href",
                "ARGO_SERVER": "my_server",
            },
            "test_namespace",
            "mytoken_without_bearer",
            "https://my_server/my_base_href/",
            None,
        ),
    ],
)
def test_authenticate(
    env, expected_namespace, expected_token, expected_host, exception
):
    with patch.dict("os.environ", env):
        if exception:
            with pytest.raises(exception):
                authenticate()
        else:
            config = authenticate()
            assert config.namespace == expected_namespace
            assert config.token == expected_token
            assert config.host == expected_host


def test_gen_papermill_command_without_conda():
    command = gen_papermill_command_input(
        conda_env_name="default",
        input_path="/home/jovyan/work/input.ipynb",
        output_path="/home/jovyan/work/output.ipynb",
        html_path="/home/jovyan/work/output.html",
        log_path="/home/jovyan/work/logs.txt",
        papermill_status_path="/home/jovyan/work/papermill_status.txt",
        use_conda_env=False,
    )

    assert "conda run" not in command
    assert "'\\''" not in command
    assert "papermill" in command
    assert "jupyter nbconvert" in command


def test_gen_pod_spec_patch_for_jupyterhub():
    patch = gen_pod_spec_patch(
        image="registry.example/notebook:1",
        service_account_name="alice-workflows",
        pvc_name="claim-alice",
        pvc_mount_path="/home/jovyan",
    )

    assert patch == (
        '{"serviceAccountName": "alice-workflows", '
        '"containers": [{"name": "main", "image": '
        '"registry.example/notebook:1", "volumeMounts": '
        '[{"name": "jupyter-user-home", "mountPath": "/home/jovyan"}]}], '
        '"volumes": [{"name": "jupyter-user-home", '
        '"persistentVolumeClaim": {"claimName": "claim-alice"}}]}'
    )


def test_gen_pod_spec_patch_includes_pod_affinity():
    affinity = {
        "podAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": [
                {
                    "labelSelector": {
                        "matchLabels": {
                            "hub.jupyter.org/username": "alice",
                            "component": "singleuser-server",
                            "release": "jupyterhub",
                        }
                    },
                    "topologyKey": "kubernetes.io/hostname",
                }
            ]
        }
    }

    patch = json.loads(gen_pod_spec_patch(pod_affinity=affinity))

    assert patch["affinity"] == affinity


def test_main_container_uses_configured_image():
    container = main_container(
        job=type("Job", (), {"runtime_environment_name": "default"})(),
        use_conda_store_env=False,
        input_path="/home/jovyan/input.ipynb",
        log_path="/home/jovyan/logs.txt",
        papermill_status_path="/home/jovyan/status.txt",
        parameters=None,
        use_conda_env=False,
        workflow_image="registry.example/notebook:1",
    )

    assert container.image == "registry.example/notebook:1"


def test_gen_cron_workflow_name_includes_definition_name():
    assert (
        gen_cron_workflow_name(
            "d66120ed-eed8-47ad-9c9e-ef58ffd1a1a3", "Scheduler Test"
        )
        == "job-def-scheduler-test-d66120ed-eed8"
    )


def test_resolve_workflow_pvc_name_from_user_label(tmp_path):
    token_path = tmp_path / "token"
    token_path.write_text("token")
    pool = Mock()
    pool.request.return_value.status = 200
    pool.request.return_value.data = json.dumps(
        {"items": [{"metadata": {"name": "claim-alice---abcd1234"}}]}
    ).encode()

    with patch.dict(
        "os.environ",
        {
            "JUPYTERHUB_USER": "alice",
            "ARGO_NAMESPACE": "jupyterhub",
            "ARGO_WORKFLOW_PVC_NAME": "",
            "KUBERNETES_SERVICE_HOST": "kubernetes.default.svc",
            "KUBERNETES_SERVICE_PORT_HTTPS": "443",
        },
        clear=False,
    ), patch(
        "argo_jupyter_scheduler.utils.KUBERNETES_SERVICE_ACCOUNT_TOKEN",
        str(token_path),
    ), patch("argo_jupyter_scheduler.utils.urllib3.PoolManager", return_value=pool):
        assert resolve_workflow_pvc_name() == "claim-alice---abcd1234"


def test_resolve_workflow_pod_affinity_from_running_user_pod(tmp_path):
    token_path = tmp_path / "token"
    token_path.write_text("token")
    pool = Mock()
    pool.request.return_value.status = 200
    pool.request.return_value.data = json.dumps(
        {
            "items": [
                {
                    "metadata": {
                        "labels": {
                            "hub.jupyter.org/username": "alice",
                            "hub.jupyter.org/servername": "",
                            "component": "singleuser-server",
                            "release": "jupyterhub",
                        }
                    },
                    "status": {"phase": "Running"},
                }
            ]
        }
    ).encode()

    with patch.dict(
        "os.environ",
        {
            "JUPYTERHUB_USER": "alice",
            "JUPYTERHUB_SERVER_NAME": "",
            "ARGO_NAMESPACE": "jupyterhub",
            "KUBERNETES_SERVICE_HOST": "kubernetes.default.svc",
            "KUBERNETES_SERVICE_PORT_HTTPS": "443",
        },
        clear=False,
    ), patch(
        "argo_jupyter_scheduler.utils.KUBERNETES_SERVICE_ACCOUNT_TOKEN",
        str(token_path),
    ), patch("argo_jupyter_scheduler.utils.urllib3.PoolManager", return_value=pool):
        affinity = resolve_workflow_pod_affinity()

    assert affinity["podAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]


def test_resolve_workflow_pod_affinity_is_empty_without_running_user_pod(tmp_path):
    token_path = tmp_path / "token"
    token_path.write_text("token")
    pool = Mock()
    pool.request.return_value.status = 200
    pool.request.return_value.data = json.dumps({"items": []}).encode()

    with patch.dict(
        "os.environ",
        {
            "JUPYTERHUB_USER": "alice",
            "ARGO_NAMESPACE": "jupyterhub",
        },
        clear=False,
    ), patch(
        "argo_jupyter_scheduler.utils.KUBERNETES_SERVICE_ACCOUNT_TOKEN",
        str(token_path),
    ), patch("argo_jupyter_scheduler.utils.urllib3.PoolManager", return_value=pool):
        assert resolve_workflow_pod_affinity() is None
