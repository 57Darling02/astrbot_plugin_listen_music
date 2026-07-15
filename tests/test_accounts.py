from __future__ import annotations

import json
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from core.accounts import (
    AccountProfile,
    AccountService,
    BilibiliAuthenticator,
    CredentialStore,
    LoginSessionAccessDenied,
    LoginSessionNotFound,
    QrLoginPoll,
    QrLoginStart,
)


class AccountServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(dir="/tmp")
        self.path = Path(self.directory.name) / "accounts.json"

    def tearDown(self) -> None:
        self.directory.cleanup()

    @staticmethod
    def _authenticator(results: list[QrLoginPoll]) -> BilibiliAuthenticator:
        polls = iter(results)

        async def start() -> QrLoginStart:
            return QrLoginStart(
                qr_url="https://example.test/login",
                poll_context="opaque-provider-token",
                expires_at=time.time() + 60,
            )

        async def poll(_: object) -> QrLoginPoll:
            return next(polls)

        async def profile(_: dict[str, str]) -> AccountProfile:
            return AccountProfile("管理员")

        return BilibiliAuthenticator(start=start, poll=poll, profile=profile)

    async def _service(self, results: list[QrLoginPoll]) -> AccountService:
        service = AccountService(
            CredentialStore(self.path),
            self._authenticator(results),
            poll_interval=0.001,
        )
        await service.restore_credentials()
        return service

    async def test_confirmed_login_persists_one_bilibili_record_without_exposing_cookies(
        self,
    ) -> None:
        service = await self._service(
            [
                QrLoginPoll("waiting", "等待扫码"),
                QrLoginPoll("scanned", "已扫码"),
                QrLoginPoll("confirmed", "登录成功", {"SESSDATA": "secret"}),
            ]
        )
        try:
            started = await service.start_login("dashboard-admin")
            events = [
                event
                async for event in service.login_events(
                    started["session_id"], "dashboard-admin"
                )
            ]

            self.assertEqual(events[0]["state"], "waiting")
            self.assertEqual(events[-1]["state"], "confirmed")
            self.assertEqual(service.cookies(), {"SESSDATA": "secret"})

            payload = await service.status_payload()
            self.assertEqual(payload["account"]["display_name"], "管理员")
            self.assertEqual(set(payload["account"]), {"state", "display_name"})
            public_json = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn("SESSDATA", public_json)
            self.assertNotIn("secret", public_json)

            stored = json.loads(self.path.read_text(encoding="utf-8"))
            self.assertEqual(set(stored), {"version", "bilibili"})
            self.assertEqual(stored["bilibili"]["cookies"], {"SESSDATA": "secret"})
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        finally:
            await service.aclose()

    async def test_qr_session_is_bound_to_the_dashboard_user(self) -> None:
        service = await self._service([QrLoginPoll("waiting")])
        try:
            started = await service.start_login("dashboard-admin")
            events = service.login_events(started["session_id"], "another-user")
            with self.assertRaises(LoginSessionAccessDenied):
                await anext(events)
        finally:
            await service.aclose()

    async def test_invalid_cookie_result_is_not_saved(self) -> None:
        service = await self._service(
            [QrLoginPoll("confirmed", cookies={"SESSDATA": "bad\r\nvalue"})]
        )
        try:
            started = await service.start_login("dashboard-admin")
            events = [
                event
                async for event in service.login_events(
                    started["session_id"], "dashboard-admin"
                )
            ]
            self.assertEqual(events[-1]["state"], "failed")
            self.assertFalse(service.has_credentials())
            self.assertFalse(self.path.exists())
        finally:
            await service.aclose()

    async def test_logout_removes_the_single_persisted_account(self) -> None:
        service = await self._service(
            [QrLoginPoll("confirmed", cookies={"SESSDATA": "token"})]
        )
        try:
            started = await service.start_login("dashboard-admin")
            async for _ in service.login_events(
                started["session_id"], "dashboard-admin"
            ):
                pass
            await service.logout()

            self.assertFalse(service.has_credentials())
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            self.assertEqual(stored, {"version": 1})
        finally:
            await service.aclose()

    async def test_new_login_discards_prior_terminal_sessions(self) -> None:
        service = await self._service(
            [
                QrLoginPoll("confirmed", cookies={"SESSDATA": "first"}),
                QrLoginPoll("confirmed", cookies={"SESSDATA": "second"}),
                QrLoginPoll("confirmed", cookies={"SESSDATA": "third"}),
            ]
        )
        try:
            first = await service.start_login("dashboard-admin")
            async for _ in service.login_events(first["session_id"], "dashboard-admin"):
                pass

            second = await service.start_login("dashboard-admin")
            with self.assertRaises(LoginSessionNotFound):
                await anext(
                    service.login_events(first["session_id"], "dashboard-admin")
                )
            async for _ in service.login_events(
                second["session_id"], "dashboard-admin"
            ):
                pass

            third = await service.start_login("dashboard-admin")
            async for _ in service.login_events(third["session_id"], "dashboard-admin"):
                pass

            self.assertEqual(set(service._sessions), {third["session_id"]})
        finally:
            await service.aclose()


if __name__ == "__main__":
    unittest.main()
