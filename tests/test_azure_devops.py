from unittest import mock

import pytest
import requests

from cronweb.azure_devops import dispatch_pipeline
from cronweb.github import DispatchResult, dispatch_workflow


@pytest.fixture
def azure_args():
    return {
        "api_url": "https://dev.azure.com/",
        "token": "fake-pat-not-a-real-secret",
        "owner": "my org",
        "repo": "Café & project",
        "workflow": "042",
        "ref": "feature/release",
        "inputs": {"environment": "prod", "options": {"flags": [1, True, None]}},
    }


def test_pipeline_request_contract(azure_args):
    with mock.patch("cronweb.azure_devops.requests.post") as post:
        post.return_value.status_code = 200
        result = dispatch_pipeline(**azure_args, timeout=17)

    post.assert_called_once_with(
        "https://dev.azure.com/my%20org/Caf%C3%A9%20%26%20project/_apis/pipelines/042/runs?api-version=7.1",
        json={
            "templateParameters": {"environment": "prod", "options": {"flags": [1, True, None]}},
            "resources": {"repositories": {"self": {"refName": "refs/heads/feature/release"}}},
        },
        auth=("", "fake-pat-not-a-real-secret"),
        timeout=17,
        allow_redirects=False,
    )
    assert result == DispatchResult(True, 200, "Pipeline dispatched")


@pytest.mark.parametrize("ref,expected", [
    ("", None),
    ("   ", None),
    ("main", "refs/heads/main"),
    ("feature/x", "refs/heads/feature/x"),
    ("refs/heads/main", "refs/heads/main"),
    ("refs/tags/v1", "refs/tags/v1"),
])
def test_pipeline_refs_and_default_timeout(azure_args, ref, expected):
    azure_args.update(ref=ref, inputs={})
    with mock.patch("cronweb.azure_devops.requests.post") as post:
        post.return_value.status_code = 200
        assert dispatch_pipeline(**azure_args).ok
    payload = {"templateParameters": {}}
    if expected is not None:
        payload["resources"] = {"repositories": {"self": {"refName": expected}}}
    assert post.call_args.kwargs["json"] == payload
    assert post.call_args.kwargs["timeout"] == 30


@pytest.mark.parametrize("status", [201, 202, 204, 301, 302, 400, 401, 403, 404, 429, 500])
def test_pipeline_non_200_status_is_sanitized_failure(azure_args, status):
    with mock.patch("cronweb.azure_devops.requests.post") as post:
        post.return_value.status_code = status
        post.return_value.text = "body contains " + azure_args["token"]
        result = dispatch_pipeline(**azure_args)
    assert result == DispatchResult(False, status, f"Azure DevOps request failed (HTTP {status})")
    assert azure_args["token"] not in result.message
    post.return_value.json.assert_not_called()


@pytest.mark.parametrize("exception", [requests.ConnectionError, requests.Timeout, UnicodeError, ValueError])
def test_pipeline_exception_does_not_disclose_credentials(azure_args, exception):
    with mock.patch("cronweb.azure_devops.requests.post", side_effect=exception(azure_args["token"])):
        result = dispatch_pipeline(**azure_args)
    assert result == DispatchResult(False, None, "Azure DevOps request failed")
    assert azure_args["token"] not in result.message


@pytest.mark.parametrize("field,value", [
    ("api_url", ""), ("api_url", "ftp://example.com"),
    ("api_url", "https://user:password@example.com"),
    ("api_url", "https://example.com?token=secret"),
    ("api_url", "https://example.com/#fragment"),
    ("api_url", "https://example.com:99999"),
    ("api_url", "https://example.com:0"),
    ("api_url", "https://example.com\\evil"),
    ("owner", ""), ("owner", "."), ("owner", ".."),
    ("owner", " org"), ("owner", "org/other"), ("owner", "org%2fother"),
    ("owner", "org\nother"), ("owner", "x" * 101),
    ("repo", ".."), ("repo", "project\\other"), ("repo", "x" * 101),
    ("workflow", "0"), ("workflow", "000"), ("workflow", "-1"),
    ("workflow", "1/2"), ("workflow", "１"), ("workflow", "1" * 256),
    ("ref", None), ("ref", " main"), ("ref", "main "),
    ("ref", "a//b"), ("ref", "../main"), ("ref", "main.lock"),
    ("ref", "main."), ("ref", "main@{x"), ("ref", "a b"),
    ("ref", "x?y"), ("ref", "x\\y"), ("ref", "x" * 256),
    ("token", ""), ("token", None), ("inputs", []),
])
def test_invalid_pipeline_configuration_never_requests(azure_args, field, value):
    azure_args[field] = value
    with mock.patch("cronweb.azure_devops.requests.post") as post:
        result = dispatch_pipeline(**azure_args)
    assert result == DispatchResult(False, None, "Invalid Azure DevOps dispatch configuration")
    post.assert_not_called()


def test_pipeline_valid_length_boundaries_and_base_path(azure_args):
    azure_args.update(api_url="http://localhost:8080/tfs/", owner="o" * 100,
                      repo="p" * 100, workflow="1" * 255, ref="r" * 255)
    with mock.patch("cronweb.azure_devops.requests.post") as post:
        post.return_value.status_code = 200
        assert dispatch_pipeline(**azure_args).ok
    assert post.call_args.args[0] == (
        "http://localhost:8080/tfs/" + "o" * 100 + "/" + "p" * 100
        + "/_apis/pipelines/" + "1" * 255 + "/runs?api-version=7.1"
    )
    assert post.call_args.kwargs["json"]["resources"]["repositories"]["self"]["refName"] == "refs/heads/" + "r" * 255


@pytest.mark.parametrize("status", [200, 204])
def test_github_dispatch_regression(status):
    with mock.patch("cronweb.github.requests.post") as post:
        post.return_value.status_code = status
        result = dispatch_workflow(api_url="https://api.github.example/", token="fake-github-token",
                                   owner="octo", repo="demo", workflow="build.yml",
                                   ref="main", inputs={"environment": "prod"})
    post.assert_called_once_with(
        "https://api.github.example/repos/octo/demo/actions/workflows/build.yml/dispatches",
        json={"ref": "main", "inputs": {"environment": "prod"}},
        headers={"Accept": "application/vnd.github+json", "Authorization": "Bearer fake-github-token",
                 "X-GitHub-Api-Version": "2022-11-28"}, timeout=30,
    )
    assert result == DispatchResult(True, status, "Workflow dispatched")


def test_github_empty_inputs_are_omitted():
    with mock.patch("cronweb.github.requests.post") as post:
        post.return_value.status_code = 204
        dispatch_workflow(api_url="https://api.github.example", token="fake", owner="octo",
                          repo="demo", workflow="42", ref="main", inputs={})
    assert post.call_args.kwargs["json"] == {"ref": "main"}