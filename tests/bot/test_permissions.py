import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from src.bot.permissions import is_bot_admin, is_bot_privileged


class IsBotAdminTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_row_true(self):
        sm = SimpleNamespace(get_user_by_id=Mock(return_value={"is_admin": 1}))
        self.assertTrue(await is_bot_admin(sm, 100))

    async def test_readonly_row_false(self):
        sm = SimpleNamespace(get_user_by_id=Mock(return_value={"is_admin": 0}))
        self.assertFalse(await is_bot_admin(sm, 100))

    async def test_no_row_false(self):
        sm = SimpleNamespace(get_user_by_id=Mock(return_value=None))
        self.assertFalse(await is_bot_admin(sm, 100))

    async def test_exception_false(self):
        sm = SimpleNamespace(get_user_by_id=Mock(side_effect=RuntimeError("db")))
        self.assertFalse(await is_bot_admin(sm, 100))

    async def test_none_manager_or_user_false(self):
        self.assertFalse(await is_bot_admin(None, 100))
        sm = SimpleNamespace(get_user_by_id=Mock(return_value={"is_admin": 1}))
        self.assertFalse(await is_bot_admin(sm, None))


class IsBotPrivilegedTests(unittest.IsolatedAsyncioTestCase):
    async def test_whitelisted_non_admin_true(self):
        # Whitelisted owner: is_admin=0 (Telegram login), but rides the
        # subscription via ALLOWED_USER_IDS. Must not touch the DB row.
        sm = SimpleNamespace(get_user_by_id=Mock(return_value={"is_admin": 0}))
        self.assertTrue(await is_bot_privileged(sm, 1166057082, [1166057082]))

    async def test_admin_user_true(self):
        # is_admin row grants privilege even when the whitelist is empty.
        sm = SimpleNamespace(get_user_by_id=Mock(return_value={"is_admin": 1}))
        self.assertTrue(await is_bot_privileged(sm, 100, []))

    async def test_random_id_false(self):
        # Neither whitelisted nor is_admin → not privileged.
        sm = SimpleNamespace(get_user_by_id=Mock(return_value={"is_admin": 0}))
        self.assertFalse(await is_bot_privileged(sm, 999, [1166057082]))

    async def test_none_allowed_list_falls_back_to_admin(self):
        sm = SimpleNamespace(get_user_by_id=Mock(return_value={"is_admin": 1}))
        self.assertTrue(await is_bot_privileged(sm, 100, None))
        sm2 = SimpleNamespace(get_user_by_id=Mock(return_value=None))
        self.assertFalse(await is_bot_privileged(sm2, 100, None))

    async def test_invalid_user_id_false(self):
        sm = SimpleNamespace(get_user_by_id=Mock(return_value={"is_admin": 1}))
        self.assertFalse(await is_bot_privileged(sm, None, [1166057082]))
        self.assertFalse(await is_bot_privileged(sm, "not-an-int", [1166057082]))
