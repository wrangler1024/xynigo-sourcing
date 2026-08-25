from __future__ import annotations

import json

import httpx

from xynigo_auth.feishu import FeishuDirectoryClient


def test_app_identity_directory_resolves_mobile_without_persisting_token() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/tenant_access_token/internal"):
            assert json.loads(request.content) == {
                "app_id": "cli_test",
                "app_secret": "test-secret-not-real",
            }
            return httpx.Response(200, json={
                "code": 0,
                "msg": "ok",
                "tenant_access_token": "tenant-token",
                "expire": 7200,
            })
        assert request.headers["Authorization"] == "Bearer tenant-token"
        if request.url.path.endswith("/batch_get_id"):
            assert request.url.params["user_id_type"] == "open_id"
            assert json.loads(request.content) == {
                "mobiles": ["13800138000"],
                "include_resigned": False,
            }
            return httpx.Response(200, json={
                "code": 0,
                "msg": "success",
                "data": {"user_list": [{"user_id": "ou_directory_user"}]},
            })
        assert request.url.path.endswith("/contact/v3/users/ou_directory_user")
        return httpx.Response(200, json={
            "code": 0,
            "msg": "success",
            "data": {"user": {
                "open_id": "ou_directory_user",
                "union_id": "on_directory_user",
                "name": "通讯录成员",
                "avatar": {"avatar_240": "https://example.test/avatar.png"},
                "department_ids": ["od_one", "od_two"],
                "status": {
                    "is_activated": True,
                    "is_frozen": False,
                    "is_resigned": False,
                    "is_exited": False,
                    "is_unjoin": False,
                },
            }},
        })

    client = FeishuDirectoryClient(
        app_id="cli_test",
        app_secret="test-secret-not-real",
        transport=httpx.MockTransport(handler),
    )
    user = client.find_user_by_mobile("13800138000")
    assert user is not None
    assert user.open_id == "ou_directory_user"
    assert user.name == "通讯录成员"
    assert user.department_ids == ("od_one", "od_two")
    assert user.is_activated is True
    assert len(requests) == 3


def test_app_identity_directory_returns_none_when_mobile_is_not_visible() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={
                "code": 0,
                "tenant_access_token": "tenant-token",
                "expire": 7200,
            })
        return httpx.Response(200, json={
            "code": 0,
            "msg": "success",
            "data": {"user_list": []},
        })

    client = FeishuDirectoryClient(
        app_id="cli_test",
        app_secret="test-secret-not-real",
        transport=httpx.MockTransport(handler),
    )
    assert client.find_user_by_mobile("+12025550123") is None
