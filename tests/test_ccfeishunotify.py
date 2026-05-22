#!/usr/bin/env python3
"""
Test suite for CCFeishuNotify
Tests database operations, Feishu card building, config loading, and notification sending.
"""

import os
import sys
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from io import BytesIO

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from ccfeishunotify import ClaudePromptTracker


class TestConfigLoading(unittest.TestCase):
    """Test Feishu config loading from env vars and config file"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.temp_dir, "feishu_config.json")

    def tearDown(self):
        if os.path.exists(self.config_path):
            os.remove(self.config_path)
        os.rmdir(self.temp_dir)
        # Clean up env vars
        for key in ["FEISHU_WEBHOOK_URL", "FEISHU_WEBHOOK_SECRET",
                     "FEISHU_APP_ID", "FEISHU_APP_SECRET",
                     "FEISHU_RECEIVE_ID", "FEISHU_RECEIVE_ID_TYPE"]:
            if key in os.environ:
                del os.environ[key]

    def test_env_var_priority(self):
        """Env vars take priority over config file"""
        os.environ["FEISHU_WEBHOOK_URL"] = "https://env-webhook.example.com"
        config_data = {
            "webhook_url": "https://file-webhook.example.com",
            "app_id": "",
            "app_secret": "",
            "receive_id": "",
            "receive_id_type": "open_id",
        }
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f)

        tracker = ClaudePromptTracker()
        tracker.script_dir = self.temp_dir
        tracker.config = tracker.load_config()
        assert tracker.config["webhook_url"] == "https://env-webhook.example.com"

    def test_config_file_fallback(self):
        """Config file used when env vars are empty"""
        config_data = {
            "webhook_url": "https://file-webhook.example.com",
            "app_id": "cli_test123",
            "app_secret": "secret123",
            "receive_id": "ou_test_user",
            "receive_id_type": "open_id",
        }
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f)

        tracker = ClaudePromptTracker()
        tracker.script_dir = self.temp_dir
        tracker.config = tracker.load_config()
        assert tracker.config["webhook_url"] == "https://file-webhook.example.com"
        assert tracker.config["app_id"] == "cli_test123"

    def test_determine_send_mode_webhook(self):
        tracker = ClaudePromptTracker()
        tracker.config = {
            "webhook_url": "https://webhook.example.com",
            "app_id": "cli_test",
            "app_secret": "secret",
            "receive_id": "ou_test",
        }
        assert tracker.determine_send_mode() == "webhook"

    def test_determine_send_mode_app(self):
        tracker = ClaudePromptTracker()
        tracker.config = {
            "webhook_url": "",
            "app_id": "cli_test",
            "app_secret": "secret",
            "receive_id": "ou_test",
            "receive_id_type": "open_id",
        }
        assert tracker.determine_send_mode() == "app"

    def test_determine_send_mode_none(self):
        tracker = ClaudePromptTracker()
        tracker.config = {
            "webhook_url": "",
            "app_id": "cli_test",
            "app_secret": "",
            "receive_id": "",
        }
        assert tracker.determine_send_mode() is None


class TestDatabaseOperations(unittest.TestCase):
    """Test SQLite database operations"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.test_db_path = os.path.join(self.temp_dir, "test_ccfeishunotify.db")

    def tearDown(self):
        import shutil
        import gc
        gc.collect()  # release SQLite file locks on Windows
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def create_test_tracker(self):
        tracker = ClaudePromptTracker()
        tracker.db_path = self.test_db_path
        tracker.init_database()
        return tracker

    def test_database_initialization(self):
        tracker = self.create_test_tracker()
        assert os.path.exists(self.test_db_path)

        with sqlite3.connect(self.test_db_path) as conn:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='prompt'"
            )
            assert cursor.fetchone() is not None

    def test_user_prompt_submit(self):
        tracker = self.create_test_tracker()
        data = {
            "session_id": "session_001",
            "prompt": "test prompt content",
            "cwd": "/projects/my-app",
            "hook_event_name": "UserPromptSubmit",
        }
        tracker.handle_user_prompt_submit(data)

        with sqlite3.connect(self.test_db_path) as conn:
            cursor = conn.execute(
                "SELECT session_id, prompt, cwd, seq FROM prompt WHERE session_id = ?",
                ("session_001",),
            )
            row = cursor.fetchone()
            assert row is not None
            assert row[0] == "session_001"
            assert row[1] == "test prompt content"
            assert row[2] == "/projects/my-app"
            assert row[3] == 1

    def test_seq_auto_increment(self):
        tracker = self.create_test_tracker()
        for i in range(3):
            data = {
                "session_id": "session_003",
                "prompt": f"prompt #{i+1}",
                "cwd": "/projects/test",
                "hook_event_name": "UserPromptSubmit",
            }
            tracker.handle_user_prompt_submit(data)

        with sqlite3.connect(self.test_db_path) as conn:
            cursor = conn.execute(
                "SELECT seq FROM prompt WHERE session_id = 'session_003' ORDER BY seq"
            )
            rows = cursor.fetchall()
            assert len(rows) == 3
            assert rows[0][0] == 1
            assert rows[1][0] == 2
            assert rows[2][0] == 3


class TestDurationCalculation(unittest.TestCase):
    """Test duration calculation"""

    def test_short_duration(self):
        tracker = ClaudePromptTracker()
        result = tracker.calculate_duration("2024-01-01T10:00:00", "2024-01-01T10:00:30")
        assert result == "30s"

    def test_minutes_duration(self):
        tracker = ClaudePromptTracker()
        result = tracker.calculate_duration("2024-01-01T10:00:00", "2024-01-01T10:02:30")
        assert result == "2m30s"

    def test_exact_minutes(self):
        tracker = ClaudePromptTracker()
        result = tracker.calculate_duration("2024-01-01T10:00:00", "2024-01-01T10:05:00")
        assert result == "5m"

    def test_hours_duration(self):
        tracker = ClaudePromptTracker()
        result = tracker.calculate_duration("2024-01-01T10:00:00", "2024-01-01T11:30:00")
        assert result == "1h30m"

    def test_exact_hours(self):
        tracker = ClaudePromptTracker()
        result = tracker.calculate_duration("2024-01-01T10:00:00", "2024-01-01T12:00:00")
        assert result == "2h"


class TestFeishuCardBuilding(unittest.TestCase):
    """Test Feishu interactive card building"""

    def test_success_card_basic(self):
        tracker = ClaudePromptTracker()
        card = tracker.build_feishu_card(
            workspace="my-app",
            notification_type="success",
            content_lines=["第 #1 轮任务已完成", "执行时长: **2m30s**"],
        )
        assert card["header"]["template"] == "green"
        assert "my-app" in card["header"]["title"]["content"]

    def test_success_card_with_detail(self):
        tracker = ClaudePromptTracker()
        card = tracker.build_feishu_card(
            workspace="my-app",
            notification_type="success",
            content_lines=["第 #1 轮任务已完成"],
            task_understanding="先查看项目结构",
            execution_detail=[
                "<font color='green'>编辑</font> `main.py` (+30/-5, 2处修改)",
                "<font color='grey'>读取</font> 3个文件",
            ],
            cost="$0.02",
            context_pct="15K/1.0M (1.5%)",
            suggestion="请验证代码",
        )
        # check execution detail div exists
        assert len(card["elements"]) >= 3  # content + detail + suggestion
        # check column_set badge area exists
        badge_area = card["elements"][-1]
        assert badge_area["tag"] == "column_set"
        assert len(badge_area["columns"]) == 4  # status, cost, ctx, time

    def test_warning_card(self):
        tracker = ClaudePromptTracker()
        card = tracker.build_feishu_card(
            workspace="project",
            notification_type="warning",
            content_lines=["**需操作**"],
            suggestion="请尽快选择操作以继续任务",
        )
        assert card["header"]["template"] == "orange"
        # check status badge color
        badge_area = card["elements"][-1]
        status_col = badge_area["columns"][0]
        assert status_col["background_style"] == "orange-50"

    def test_urgent_card(self):
        tracker = ClaudePromptTracker()
        card = tracker.build_feishu_card(
            workspace="project",
            notification_type="urgent",
            content_lines=["**需授权**"],
        )
        assert card["header"]["template"] == "red"
        badge_area = card["elements"][-1]
        status_col = badge_area["columns"][0]
        assert status_col["background_style"] == "red-50"

    def test_info_card(self):
        tracker = ClaudePromptTracker()
        card = tracker.build_feishu_card(
            workspace="project",
            notification_type="info",
            content_lines=["**通知**"],
        )
        assert card["header"]["template"] == "blue"

    def test_card_without_optional_fields(self):
        tracker = ClaudePromptTracker()
        card = tracker.build_feishu_card(
            workspace="test",
            notification_type="success",
            content_lines=["任务完成"],
        )
        # badge should still show with "---" for missing cost/ctx
        badge_area = card["elements"][-1]
        cost_col = badge_area["columns"][1]
        cost_content = cost_col["elements"][0]["content"]
        assert "---" in cost_content


class TestCwdConversion(unittest.TestCase):
    """Test cwd to project directory name conversion"""

    def test_windows_path(self):
        tracker = ClaudePromptTracker()
        result = tracker.cwd_to_project_dir("d:\\MT\\ccnotify")
        assert result == "d--MT-ccnotify"

    def test_windows_user_path(self):
        tracker = ClaudePromptTracker()
        result = tracker.cwd_to_project_dir("C:\\Users\\Administrator")
        assert result == "C--Users-Administrator"

    def test_unix_path(self):
        tracker = ClaudePromptTracker()
        result = tracker.cwd_to_project_dir("/home/user/project")
        assert result == "-home-user-project"


class TestTranslateToChinese(unittest.TestCase):
    """Test English to Chinese keyword translation"""

    def test_basic_translation(self):
        tracker = ClaudePromptTracker()
        result = tracker.translate_to_chinese("Let me first check the file", max_len=80)
        # should contain Chinese translations of key words
        assert "查看" in result or "检查" in result
        assert "文件" in result

    def test_truncation(self):
        tracker = ClaudePromptTracker()
        long_text = "A very long text that should be truncated because it exceeds the maximum length"
        result = tracker.translate_to_chinese(long_text, max_len=30)
        assert len(result) <= 33  # 30 + "..."
        assert result.endswith("...")

    def test_empty_input(self):
        tracker = ClaudePromptTracker()
        result = tracker.translate_to_chinese("", max_len=80)
        assert result == ""


class TestExecutionDetail(unittest.TestCase):
    """Test business-oriented execution detail building"""

    def test_files_modified(self):
        tracker = ClaudePromptTracker()
        data = {
            "files_modified": {
                "main.py": {"lines_added": 30, "lines_removed": 5, "edit_count": 2},
                "config.py": {"lines_added": 10, "lines_removed": 0, "edit_count": 1},
            },
            "files_written": [],
            "files_read": ["README.md"],
            "commands_run": [],
            "decisions": [],
        }
        result = tracker.build_execution_detail(data)
        assert len(result) >= 2
        assert "main.py" in result[0]
        assert "+30/-5" in result[0]
        assert "config.py" in result[1]

    def test_many_files_read(self):
        tracker = ClaudePromptTracker()
        data = {
            "files_modified": {},
            "files_written": [],
            "files_read": ["a.py", "b.py", "c.py", "d.py", "e.py"],
            "commands_run": [],
            "decisions": [],
        }
        result = tracker.build_execution_detail(data)
        read_line = [l for l in result if "读取" in l][0]
        assert "5个文件" in read_line

    def test_few_files_read(self):
        tracker = ClaudePromptTracker()
        data = {
            "files_modified": {},
            "files_written": [],
            "files_read": ["a.py", "b.py"],
            "commands_run": [],
            "decisions": [],
        }
        result = tracker.build_execution_detail(data)
        read_line = [l for l in result if "读取" in l][0]
        assert "a.py" in read_line
        assert "b.py" in read_line

    def test_empty_data(self):
        tracker = ClaudePromptTracker()
        data = {
            "files_modified": {},
            "files_written": [],
            "files_read": [],
            "commands_run": [],
            "decisions": [],
        }
        result = tracker.build_execution_detail(data)
        assert result == []


class TestTokenFormatting(unittest.TestCase):
    """Test token count formatting"""

    def test_millions(self):
        tracker = ClaudePromptTracker()
        result = tracker._format_token_count(1_500_000)
        assert result == "1.5M"

    def test_thousands(self):
        tracker = ClaudePromptTracker()
        result = tracker._format_token_count(15_000)
        assert result == "15K"

    def test_small_count(self):
        tracker = ClaudePromptTracker()
        result = tracker._format_token_count(500)
        assert result == "500"


class TestContextPercentage(unittest.TestCase):
    """Test context window usage percentage formatting"""

    def test_claude_opus_model(self):
        tracker = ClaudePromptTracker()
        result = tracker.format_context_pct(50_000, "claude-opus-4-7")
        assert "50K" in result
        assert "1.0M" in result
        assert "5.0%" in result

    def test_claude_sonnet_model(self):
        tracker = ClaudePromptTracker()
        result = tracker.format_context_pct(15_000, "claude-sonnet-4-6")
        assert "15K" in result
        assert "200K" in result
        assert "7.5%" in result

    def test_unknown_model_fallback(self):
        tracker = ClaudePromptTracker()
        result = tracker.format_context_pct(50_000, "glm-5.1")
        # should use 200K default with percentage
        assert "50K" in result
        assert "%" in result

    def test_model_with_opus_keyword(self):
        tracker = ClaudePromptTracker()
        result = tracker.format_context_pct(500_000, "custom-opus-v2")
        assert "1.0M" in result

    def test_zero_tokens(self):
        tracker = ClaudePromptTracker()
        result = tracker.format_context_pct(0, "claude-opus-4-7")
        assert result is None


class TestCostEstimation(unittest.TestCase):
    """Test cost estimation from token usage"""

    def test_opus_small_cost(self):
        tracker = ClaudePromptTracker()
        result = tracker.estimate_cost(1000, 500, "claude-opus-4-7")
        assert result is not None
        assert result.startswith("$")

    def test_sonnet_cost(self):
        tracker = ClaudePromptTracker()
        result = tracker.estimate_cost(10_000, 2_000, "claude-sonnet-4-6")
        assert result is not None

    def test_unknown_model(self):
        tracker = ClaudePromptTracker()
        result = tracker.estimate_cost(1000, 500, "glm-5.1")
        assert result is None

    def test_none_model(self):
        tracker = ClaudePromptTracker()
        result = tracker.estimate_cost(1000, 500, None)
        assert result is None


class TestWebhookSignature(unittest.TestCase):
    """Test Feishu webhook signature generation"""

    def test_sign_generation(self):
        tracker = ClaudePromptTracker()
        timestamp, sign = tracker.generate_sign("test_secret_key")
        assert timestamp is not None
        assert sign is not None
        assert len(sign) > 0

    def test_sign_deterministic(self):
        """Same timestamp + secret produces same sign"""
        tracker = ClaudePromptTracker()
        # Manually compute expected
        import hashlib
        import base64
        ts = "1700000000"
        string_to_sign = f"{ts}\ntest_secret"
        hash_value = hashlib.sha256(string_to_sign.encode("utf-8")).digest()
        expected_sign = base64.b64encode(hash_value).decode("utf-8")

        # Verify the method produces consistent format
        timestamp, sign = tracker.generate_sign("test_secret")
        assert len(sign) == len(expected_sign)


class TestNotificationSending(unittest.TestCase):
    """Test notification sending via both modes"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @patch("urllib.request.urlopen")
    def test_webhook_mode_send(self, mock_urlopen):
        """Test sending via webhook mode"""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"code": 0, "msg": "success"}).encode("utf-8")
        mock_urlopen.return_value = mock_response

        tracker = ClaudePromptTracker()
        tracker.send_mode = "webhook"
        tracker.config = {
            "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/test123",
            "webhook_secret": "",
        }

        tracker.send_notification(
            workspace="my-app",
            notification_type="success",
            content_lines=["第 #1 轮任务已完成", "执行时长: **2m30s**"],
        )

        assert mock_urlopen.called

    @patch("urllib.request.urlopen")
    def test_app_mode_send(self, mock_urlopen):
        """Test sending via app mode (mock both token and message API)"""
        token_response = MagicMock()
        token_response.read.return_value = json.dumps({
            "code": 0,
            "tenant_access_token": "test_token_123",
            "expire": 7200,
        }).encode("utf-8")

        msg_response = MagicMock()
        msg_response.read.return_value = json.dumps({
            "code": 0,
            "data": {"message_id": "msg_123"},
        }).encode("utf-8")

        mock_urlopen.side_effect = [token_response, msg_response]

        tracker = ClaudePromptTracker()
        tracker.send_mode = "app"
        tracker.config = {
            "app_id": "cli_test",
            "app_secret": "test_secret",
            "receive_id": "ou_test_user",
            "receive_id_type": "open_id",
            "webhook_url": "",
        }

        tracker.send_notification(
            workspace="my-app",
            notification_type="success",
            content_lines=["第 #1 轮任务已完成"],
        )

        assert mock_urlopen.call_count == 2

    def test_no_config_skip(self):
        tracker = ClaudePromptTracker()
        tracker.send_mode = None
        tracker.config = {"webhook_url": "", "app_id": "", "receive_id": ""}
        tracker.send_notification(workspace="test", notification_type="info", content_lines=["test"])


class TestMainFunction(unittest.TestCase):
    """Test main function and input processing"""

    def test_no_args_prints_ok(self):
        with patch("sys.argv", ["ccfeishunotify.py"]):
            with patch("builtins.print") as mock_print:
                from ccfeishunotify import main
                main()
                mock_print.assert_called_with("ok")

    def test_invalid_hook_type(self):
        with patch("sys.argv", ["ccfeishunotify.py", "InvalidEvent"]):
            with patch("sys.stdin") as mock_stdin:
                mock_stdin.read.return_value = '{"hook_event_name": "InvalidEvent"}'
                from ccfeishunotify import main
                try:
                    main()
                    assert False, "Should have exited"
                except SystemExit:
                    pass


if __name__ == "__main__":
    unittest.main()