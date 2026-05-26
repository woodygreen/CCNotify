#!/usr/bin/env python3
"""
Claude Code Feishu Notify
Send Claude Code notifications to Feishu (Lark) via App API or custom bot webhook.
Based on CCNotify (https://github.com/dazuiba/CCNotify)

Supports two modes:
1. App mode: uses app_id + app_secret to get tenant_access_token, sends via IM API
2. Webhook mode: uses custom bot webhook URL directly (simpler setup)
"""

import os
import sys
import json
import re
import sqlite3
import hashlib
import base64
import time
import urllib.request
import urllib.error
import logging
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime


FEISHU_APP_API_BASE = "https://open.feishu.cn/open-apis"

FEISHU_WEBHOOK_URL_ENV = "FEISHU_WEBHOOK_URL"
FEISHU_WEBHOOK_SECRET_ENV = "FEISHU_WEBHOOK_SECRET"
FEISHU_APP_ID_ENV = "FEISHU_APP_ID"
FEISHU_APP_SECRET_ENV = "FEISHU_APP_SECRET"
FEISHU_RECEIVE_ID_ENV = "FEISHU_RECEIVE_ID"
FEISHU_RECEIVE_ID_TYPE_ENV = "FEISHU_RECEIVE_ID_TYPE"

AI_BASE_URL_ENV = "ANTHROPIC_BASE_URL"
AI_API_KEY_ENV = "ANTHROPIC_API_KEY"

# simple English→Chinese keyword map for task understanding
EN_ZH_MAP = {
    "let me": "先",
    "i'll": "将",
    "i will": "将",
    "first": "首先",
    "then": "然后",
    "next": "接着",
    "look at": "查看",
    "check": "检查",
    "review": "审查",
    "read": "读取",
    "examine": "分析",
    "analyze": "分析",
    "investigate": "调查",
    "implement": "实现",
    "add": "添加",
    "remove": "移除",
    "delete": "删除",
    "update": "更新",
    "modify": "修改",
    "change": "修改",
    "fix": "修复",
    "optimize": "优化",
    "refactor": "重构",
    "improve": "改进",
    "enhance": "增强",
    "create": "创建",
    "build": "构建",
    "configure": "配置",
    "setup": "设置",
    "install": "安装",
    "deploy": "部署",
    "test": "测试",
    "debug": "调试",
    "search": "搜索",
    "find": "查找",
    "explore": "探索",
    "verify": "验证",
    "validate": "验证",
    "ensure": "确保",
    "handle": "处理",
    "process": "处理",
    "extract": "提取",
    "parse": "解析",
    "generate": "生成",
    "convert": "转换",
    "transform": "转换",
    "calculate": "计算",
    "estimate": "估算",
    "compare": "比较",
    "merge": "合并",
    "split": "拆分",
    "replace": "替换",
    "move": "移动",
    "copy": "复制",
    "rename": "重命名",
    "the": "",
    "a": "",
    "an": "",
    "this": "此",
    "that": "该",
    "file": "文件",
    "files": "文件",
    "code": "代码",
    "function": "函数",
    "method": "方法",
    "class": "类",
    "module": "模块",
    "variable": "变量",
    "config": "配置",
    "data": "数据",
    "error": "错误",
    "bug": "bug",
    "issue": "问题",
    "problem": "问题",
    "feature": "功能",
    "logic": "逻辑",
    "structure": "结构",
    "format": "格式",
    "content": "内容",
    "layout": "布局",
    "design": "设计",
    "approach": "方案",
    "solution": "方案",
    "card": "卡片",
    "notification": "通知",
    "summary": "摘要",
    "detail": "详情",
    "result": "结果",
    "output": "输出",
    "input": "输入",
    "process": "过程",
}


class ClaudePromptTracker:
    def __init__(self):
        """Initialize the prompt tracker with database and Feishu config"""
        script_dir = os.path.dirname(os.path.abspath(__file__))
        # if script lives in a src/ subdirectory, project root is parent
        if os.path.basename(script_dir) == "src":
            project_dir = os.path.dirname(script_dir)
        else:
            project_dir = script_dir
        self.db_path = os.path.join(project_dir, "db", "ccfeishunotify.db")
        self.project_dir = project_dir
        # ensure subdirectories exist
        for subdir in ("db", "logs", "configs"):
            os.makedirs(os.path.join(project_dir, subdir), exist_ok=True)

        # Load config and determine mode
        self.config = self.load_config()
        self.send_mode = self.determine_send_mode()

        self.setup_logging()
        self.init_database()

        # Token cache for app mode
        self._tenant_token = None
        self._token_expire_at = 0

    def load_config(self):
        """Load Feishu config from env vars or config file"""
        config = {
            "webhook_url": "",
            "webhook_secret": "",
            "app_id": "",
            "app_secret": "",
            "receive_id": "",
            "receive_id_type": "",
            "ai_base_url": "",
            "ai_api_key": "",
            "ai_model": "claude-sonnet-4-6",
        }

        # Priority 2: config file (applied first, lowest priority)
        config_path = os.path.join(self.project_dir, "configs", "feishu_config.json")
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    file_config = json.load(f)
                for key in config:
                    if file_config.get(key):
                        config[key] = file_config[key]
            except Exception as e:
                logging.error(f"Error reading config file {config_path}: {e}")

        # Priority 1: environment variables (override config file)
        env_mapping = {
            "webhook_url": FEISHU_WEBHOOK_URL_ENV,
            "webhook_secret": FEISHU_WEBHOOK_SECRET_ENV,
            "app_id": FEISHU_APP_ID_ENV,
            "app_secret": FEISHU_APP_SECRET_ENV,
            "receive_id": FEISHU_RECEIVE_ID_ENV,
            "receive_id_type": FEISHU_RECEIVE_ID_TYPE_ENV,
            "ai_base_url": AI_BASE_URL_ENV,
            "ai_api_key": AI_API_KEY_ENV,
        }
        for key, env_var in env_mapping.items():
            val = os.environ.get(env_var)
            if val:
                config[key] = val

        # Apply default for receive_id_type if still empty
        if not config["receive_id_type"]:
            config["receive_id_type"] = "open_id"

        return config

    def determine_send_mode(self):
        """Determine which send mode to use based on config"""
        cfg = self.config
        if cfg.get("webhook_url"):
            return "webhook"
        if cfg.get("app_id") and cfg.get("app_secret") and cfg.get("receive_id"):
            return "app"
        return None

    def setup_logging(self):
        """Setup logging to file with daily rotation"""
        log_path = os.path.join(self.project_dir, "logs", "ccfeishunotify.log")

        handler = TimedRotatingFileHandler(
            log_path,
            when="midnight",
            interval=1,
            backupCount=1,
            encoding="utf-8",
            errors="replace",
        )
        formatter = logging.Formatter(
            "%(asctime)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        )
        handler.setFormatter(formatter)

        logger = logging.getLogger()
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)

    def init_database(self):
        """Create tables and triggers if they don't exist"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS prompt (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    prompt TEXT,
                    cwd TEXT,
                    seq INTEGER,
                    stoped_at DATETIME,
                    lastWaitUserAt DATETIME,
                    cumulative_cost REAL
                )
            """)
            # add cumulative_cost column to existing tables
            try:
                conn.execute("ALTER TABLE prompt ADD COLUMN cumulative_cost REAL")
            except sqlite3.OperationalError:
                pass  # column already exists
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS auto_increment_seq
                AFTER INSERT ON prompt
                FOR EACH ROW
                BEGIN
                    UPDATE prompt
                    SET seq = (
                        SELECT COALESCE(MAX(seq), 0) + 1
                        FROM prompt
                        WHERE session_id = NEW.session_id
                    )
                    WHERE id = NEW.id;
                END
            """)
            conn.commit()

    def handle_user_prompt_submit(self, data):
        """Handle UserPromptSubmit event - insert new prompt record"""
        session_id = data.get("session_id")
        prompt = data.get("prompt", "")
        cwd = data.get("cwd", "")

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO prompt (session_id, prompt, cwd) VALUES (?, ?, ?)",
                (session_id, prompt, cwd),
            )
            conn.commit()

        logging.info(f"Recorded prompt for session {session_id}")

    def handle_stop(self, data):
        """Handle Stop event - update completion time and send Feishu notification"""
        session_id = data.get("session_id")
        cost_usd = data.get("costUSD")

        logging.info(f"[STOP] raw data keys: {list(data.keys())}")

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                SELECT id, created_at, cwd, prompt
                FROM prompt
                WHERE session_id = ? AND stoped_at IS NULL
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (session_id,),
            )

            row = cursor.fetchone()
            if row:
                record_id, created_at, cwd, prompt = row

                conn.execute(
                    "UPDATE prompt SET stoped_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (record_id,),
                )
                conn.commit()

                cursor = conn.execute(
                    "SELECT seq FROM prompt WHERE id = ?", (record_id,)
                )
                seq_row = cursor.fetchone()
                seq = seq_row[0] if seq_row else 1

                duration = self.calculate_duration_from_db(record_id)
                workspace = os.path.basename(cwd) if cwd else "unknown"

                # prefer transcript_path from hook data, fallback to computed path
                transcript_path = data.get("transcript_path") or (self.find_transcript_path(session_id, cwd) if cwd else None)
                transcript_data = self.parse_transcript_summary(transcript_path) if transcript_path else None

                # build card content from transcript
                task_understanding = ""
                execution_detail = ""
                display_cost = None          # per-round cost string
                display_cumulative_cost = None  # session cumulative cost string
                context_pct = None
                suggestion = "请检查结果，决定下一步操作"

                if transcript_data:
                    # task understanding from thinking block
                    raw_understanding = transcript_data.get("task_understanding", "")
                    if raw_understanding:
                        task_understanding = raw_understanding

                    # execution detail (business-oriented, with font colors)
                    execution_detail = self.build_execution_detail(transcript_data)

                    # context usage percentage
                    context_pct = self.format_context_pct(
                        transcript_data["max_input_tokens"], transcript_data["model"]
                    )

                    # cost estimation (cumulative from transcript tokens)
                    cumulative_cost_val = None
                    if cost_usd:
                        cumulative_cost_val = cost_usd
                    else:
                        estimated = self.estimate_cost(
                            transcript_data["total_input_tokens"],
                            transcript_data["total_output_tokens"],
                            transcript_data["model"],
                            transcript_data["total_cache_read_tokens"],
                            transcript_data["total_cache_creation_tokens"],
                        )
                        if estimated != "unknown":
                            try:
                                cumulative_cost_val = float(estimated.replace("$", ""))
                            except ValueError:
                                cumulative_cost_val = None

                    # get previous cumulative cost for this session to compute per-round
                    prev_cumulative = 0
                    cursor = conn.execute(
                        "SELECT cumulative_cost FROM prompt WHERE session_id = ? AND cumulative_cost IS NOT NULL ORDER BY id DESC LIMIT 1",
                        (session_id,),
                    )
                    prev_row = cursor.fetchone()
                    if prev_row and prev_row[0]:
                        prev_cumulative = prev_row[0]

                    if cumulative_cost_val is not None:
                        per_round_val = cumulative_cost_val - prev_cumulative
                        display_cost = self.format_cost_value(per_round_val)
                        display_cumulative_cost = self.format_cost_value(cumulative_cost_val)
                        # store cumulative cost in DB
                        conn.execute(
                            "UPDATE prompt SET cumulative_cost = ? WHERE id = ?",
                            (cumulative_cost_val, record_id),
                        )
                        conn.commit()
                    elif cost_usd is None and not transcript_data:
                        display_cost = "unknown"
                        display_cumulative_cost = "unknown"

                    # handle hook-provided costUSD (cumulative) when no transcript
                    if cost_usd and not cumulative_cost_val:
                        cumulative_cost_val = cost_usd
                        cursor = conn.execute(
                            "SELECT cumulative_cost FROM prompt WHERE session_id = ? AND cumulative_cost IS NOT NULL ORDER BY id DESC LIMIT 1",
                            (session_id,),
                        )
                        prev_row = cursor.fetchone()
                        prev_cumulative = prev_row[0] if prev_row and prev_row[0] else 0
                        per_round_val = cumulative_cost_val - prev_cumulative
                        display_cost = self.format_cost_value(per_round_val)
                        display_cumulative_cost = self.format_cost_value(cumulative_cost_val)
                        conn.execute(
                            "UPDATE prompt SET cumulative_cost = ? WHERE id = ?",
                            (cumulative_cost_val, record_id),
                        )
                        conn.commit()

                    # smart suggestion (keep original, AI summary updates via queue)
                    raw_suggestion = transcript_data.get("last_suggestion", "")
                    if raw_suggestion:
                        suggestion = raw_suggestion

                # content_lines no longer include duration (moved to badge)
                content_lines = [f"第 `#{seq}` 轮任务已完成"]

                sent, message_id = self.send_notification(
                    workspace=workspace,
                    notification_type="success",
                    content_lines=content_lines,
                    task_understanding=task_understanding,
                    execution_detail=execution_detail,
                    cost=display_cost,
                    cumulative_cost=display_cumulative_cost,
                    context_pct=context_pct,
                    suggestion=suggestion,
                    model=transcript_data.get("model") if transcript_data else None,
                    duration=duration,
                    seq=seq,
                )

                status = "card sent" if sent else "card send FAILED"
                logging.info(
                    f"[{workspace}] job#{seq} done, duration={duration}, cost={display_cost}/{display_cumulative_cost}, msg_id={message_id}, {status}"
                )

                # save raw data to summary queue for later AI processing
                if transcript_data and sent:
                    self.save_summary_queue(
                        session_id=session_id,
                        workspace=workspace,
                        seq=seq,
                        message_id=message_id,
                        duration=duration,
                        cost=display_cost,
                        cumulative_cost=display_cumulative_cost,
                        context_pct=context_pct,
                        transcript_data=transcript_data,
                    )

    def handle_notification(self, data):
        """Handle Notification event - check types and send Feishu notification"""
        session_id = data.get("session_id")
        message = data.get("message", "")
        cwd = data.get("cwd", "")

        logging.info(f"[NOTIFICATION] session={session_id}, message='{message}'")

        message_lower = message.lower()
        workspace = os.path.basename(cwd) if cwd else "unknown"
        should_update_db = False
        should_notify = True
        notification_type = "info"
        label = ""
        suggestion = ""

        if "waiting for your input" in message_lower or "waiting for input" in message_lower:
            label = "等待输入"
            should_update_db = True
            should_notify = False
            notification_type = "warning"
            suggestion = "请回到 Claude Code 查看并回复"
        elif "permission" in message_lower:
            label = "需要授权"
            notification_type = "urgent"
            suggestion = "请尽快授权以继续任务"
        elif "approval" in message_lower or "choose an option" in message_lower:
            label = "需要操作"
            notification_type = "warning"
            suggestion = "请尽快选择操作以继续任务"
        else:
            label = "通知"
            suggestion = "请查看 Claude Code"

        if should_update_db:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    UPDATE prompt
                    SET lastWaitUserAt = CURRENT_TIMESTAMP
                    WHERE id = (
                        SELECT id FROM prompt
                        WHERE session_id = ?
                        ORDER BY created_at DESC
                        LIMIT 1
                    )
                    """,
                    (session_id,),
                )
                conn.commit()
            logging.info(f"Updated lastWaitUserAt for session {session_id}")

        if should_notify:
            content_lines = [f"**{label}**"]
            content_lines.append(f"提示: {message}")
            sent, _ = self.send_notification(
                workspace=workspace,
                notification_type=notification_type,
                content_lines=content_lines,
                suggestion=suggestion,
            )
            status = "card sent" if sent else "card send FAILED"
            logging.info(f"[{workspace}] {label}, {status}")
        else:
            logging.info(f"[{workspace}] {label} (suppressed, will notify on Stop)")

    def calculate_duration_from_db(self, record_id):
        """Calculate duration for a completed record"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT created_at, stoped_at FROM prompt WHERE id = ?",
                (record_id,),
            )
            row = cursor.fetchone()
            if row and row[1]:
                return self.calculate_duration(row[0], row[1])

        return "Unknown"

    def calculate_duration(self, start_time, end_time):
        """Calculate human-readable duration between two timestamps"""
        try:
            if isinstance(start_time, str):
                start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            else:
                start_dt = datetime.fromisoformat(start_time)

            if isinstance(end_time, str):
                end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
            else:
                end_dt = datetime.fromisoformat(end_time)

            duration = end_dt - start_dt
            total_seconds = int(duration.total_seconds())

            if total_seconds < 60:
                return f"{total_seconds}s"
            elif total_seconds < 3600:
                minutes = total_seconds // 60
                seconds = total_seconds % 60
                if seconds > 0:
                    return f"{minutes}m{seconds}s"
                else:
                    return f"{minutes}m"
            else:
                hours = total_seconds // 3600
                minutes = (total_seconds % 3600) // 60
                if minutes > 0:
                    return f"{hours}h{minutes}m"
                else:
                    return f"{hours}h"
        except Exception as e:
            logging.error(f"Error calculating duration: {e}")
            return "Unknown"

    def generate_sign(self, secret):
        """Generate Feishu webhook signature for request verification"""
        timestamp = str(int(time.time()))
        string_to_sign = f"{timestamp}\n{secret}"
        hash_value = hashlib.sha256(string_to_sign.encode("utf-8")).digest()
        sign = base64.b64encode(hash_value).decode("utf-8")
        return timestamp, sign

    def cwd_to_project_dir(self, cwd):
        """Convert cwd path to Claude Code project directory name format"""
        # Claude Code uses lowercase drive letter on Windows
        result = cwd.replace(":", "-").replace("\\", "-").replace("/", "-")
        if len(result) >= 2 and result[0].isalpha() and result[1] == "-":
            result = result[0].lower() + result[1:]
        return result

    def find_transcript_path(self, session_id, cwd):
        """Find the Claude Code transcript JSONL file for a session"""
        if not cwd:
            return None
        project_dir = self.cwd_to_project_dir(cwd)
        home = os.path.expanduser("~")
        transcript_path = os.path.join(
            home, ".claude", "projects", project_dir, f"{session_id}.jsonl"
        )
        if os.path.exists(transcript_path):
            return transcript_path
        return None

    def parse_transcript_summary(self, transcript_path):
        """Parse transcript JSONL to extract rich execution data"""
        total_input_tokens = 0
        total_output_tokens = 0
        total_cache_read_tokens = 0
        total_cache_creation_tokens = 0
        max_input_tokens = 0
        model = None
        seen_msg_ids = set()

        # rich data extraction
        task_understanding = ""   # from thinking block (model's understanding of task)
        first_thinking_found = False
        last_suggestion = ""
        files_modified = {}   # {basename: {lines_added, lines_removed, edit_count}}
        files_written = []
        files_read = []
        commands_run = []
        decisions = []

        try:
            with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if data.get("type") != "assistant":
                        continue

                    msg = data.get("message", {})
                    if not msg:
                        continue

                    # extract model
                    msg_model = msg.get("model")
                    if msg_model and not model:
                        model = msg_model

                    # deduplicate token usage by message ID
                    msg_id = msg.get("id")
                    if msg_id and msg_id not in seen_msg_ids:
                        seen_msg_ids.add(msg_id)
                        usage = msg.get("usage", {})
                        if usage:
                            it = usage.get("input_tokens", 0) or 0
                            ot = usage.get("output_tokens", 0) or 0
                            cr = usage.get("cache_read_input_tokens", 0) or 0
                            cc = usage.get("cache_creation_input_tokens", 0) or 0
                            total_input_tokens += it
                            total_output_tokens += ot
                            total_cache_read_tokens += cr
                            total_cache_creation_tokens += cc
                            if it > max_input_tokens:
                                max_input_tokens = it

                    # extract content details
                    content = msg.get("content", [])
                    if isinstance(content, list):
                        for item in content:
                            if not isinstance(item, dict):
                                continue

                            # extract thinking content (task understanding source)
                            if item.get("type") == "thinking":
                                thinking_text = item.get("thinking", "")
                                if thinking_text and len(thinking_text) > 15:
                                    if not first_thinking_found:
                                        task_understanding = thinking_text
                                        first_thinking_found = True

                            # extract assistant text (reasoning, decisions, suggestions)
                            elif item.get("type") == "text":
                                text = item.get("text", "")
                                if text and len(text) > 30:
                                    decisions.append(text)
                                if text:
                                    last_suggestion = text

                            # extract tool calls with detail
                            elif item.get("type") == "tool_use":
                                tool_name = item.get("name", "unknown")
                                tool_input = item.get("input", {})

                                if tool_name == "Edit":
                                    fp = tool_input.get("file_path", "")
                                    old_str = tool_input.get("old_string", "")
                                    new_str = tool_input.get("new_string", "")
                                    basename = os.path.basename(fp) if fp else "unknown"
                                    old_lines = len(old_str.split("\n")) if old_str else 0
                                    new_lines = len(new_str.split("\n")) if new_str else 0
                                    if basename not in files_modified:
                                        files_modified[basename] = {
                                            "lines_added": 0, "lines_removed": 0,
                                            "edit_count": 0,
                                        }
                                    files_modified[basename]["lines_added"] += new_lines
                                    files_modified[basename]["lines_removed"] += old_lines
                                    files_modified[basename]["edit_count"] += 1

                                elif tool_name == "Write":
                                    fp = tool_input.get("file_path", "")
                                    if fp:
                                        files_written.append(os.path.basename(fp))

                                elif tool_name in ("Read", "Grep", "Glob"):
                                    fp = tool_input.get("file_path", "")
                                    if fp:
                                        basename = os.path.basename(fp)
                                        if basename not in files_read:
                                            files_read.append(basename)

                                elif tool_name in ("Bash", "PowerShell"):
                                    desc = tool_input.get("description", "")
                                    cmd = tool_input.get("command", "")
                                    if desc and len(desc) > 3:
                                        commands_run.append(desc)
                                    elif cmd:
                                        commands_run.append(cmd)

        except Exception as e:
            logging.error(f"Error parsing transcript {transcript_path}: {e}")
            return None

        return {
            "task_understanding": task_understanding,
            "decisions": decisions,
            "last_suggestion": last_suggestion,
            "files_modified": files_modified,
            "files_written": files_written,
            "files_read": files_read,
            "commands_run": commands_run,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_cache_read_tokens": total_cache_read_tokens,
            "total_cache_creation_tokens": total_cache_creation_tokens,
            "max_input_tokens": max_input_tokens,
            "model": model,
        }

    def ai_summarize(self, text, purpose="general", fallback=None):
        """Use Anthropic API to generate a concise Chinese summary.

        Falls back to keyword translation if AI is unavailable or fails.
        """
        if not text:
            return ""
        cfg = self.config
        base_url = cfg.get("ai_base_url")
        api_key = cfg.get("ai_api_key")
        model = cfg.get("ai_model", "claude-sonnet-4-6")

        if not base_url or not api_key:
            return self.translate_to_chinese(text) if not fallback else fallback

        prompts = {
            "task": "将以下内容用一句精简的中文概括任务目标，言简意赅，不要啰嗦，不要省略号",
            "command": "将以下命令描述用一句精简中文概括做了什么，言简意赅",
            "decision": "将以下推理内容用一句精简中文概括核心决策，言简意赅，只说结论",
            "suggestion": "将以下建议用一句精简中文概括，言简意赅",
            "change": "将以下代码改动用精简中文逐条概括每项改动的目的，用编号列表格式，每条言简意赅",
            "general": "将以下内容用一句精简的中文概括，言简意赅",
        }
        sys_prompt = prompts.get(purpose, prompts["general"])

        try:
            url = f"{base_url}/v1/messages"
            payload = {
                "model": model,
                "max_tokens": 200,
                "messages": [{"role": "user", "content": f"{sys_prompt}\n\n{text}"},
                ],
            }
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers = {
                "Content-Type": "application/json; charset=utf-8",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            }
            req = urllib.request.Request(url, data=data, headers=headers)
            response = urllib.request.urlopen(req, timeout=15)
            result = json.loads(response.read().decode("utf-8"))
            content_blocks = result.get("content", [])
            if content_blocks:
                summary = content_blocks[0].get("text", "").strip()
                if summary:
                    return summary
        except Exception as e:
            logging.warning(f"AI summarize failed ({purpose}): {e}")

        # fallback: keyword translation
        return self.translate_to_chinese(text) if not fallback else fallback

    def translate_to_chinese(self, text):
        """Translate English text to Chinese with keyword replacement"""
        if not text:
            return ""
        result = text.lower()
        # simple keyword replacement (longer phrases first to avoid partial matches)
        sorted_keys = sorted(EN_ZH_MAP.keys(), key=len, reverse=True)
        for en in sorted_keys:
            zh = EN_ZH_MAP[en]
            if zh == "":
                # remove filler words
                result = result.replace(en + " ", " ")
                result = result.replace(" " + en, " ")
            else:
                result = result.replace(en, zh)
        # clean up extra spaces
        result = " ".join(result.split())
        return result

    def summarize_text(self, text, max_chars=20):
        """Condense text to a short Chinese summary (~max_chars 字).

        Takes the first meaningful clause, translates it, and truncates
        if still too long. No mechanical splitting — just one concise line.
        """
        if not text:
            return ""
        # take first sentence/clause only
        first = text.split(".")[0].split(",")[0].strip()
        translated = self.translate_to_chinese(first)
        if len(translated) <= max_chars:
            return translated
        # hard truncate with ellipsis
        return translated[:max_chars] + "…"

    def format_as_bullets(self, text, max_chars=30):
        """Format text as numbered bullets split at punctuation, each on its own line."""
        if not text:
            return text
        # split at Chinese/English punctuation
        segments = []
        for seg in re.split(r'[。！？；，.!?,;]', text):
            s = seg.strip()
            if s:
                segments.append(s)
        if len(segments) <= 1:
            return text
        return "\n".join([f"{i + 1}. {s}" for i, s in enumerate(segments)])

    def strip_markdown_headers(self, text):
        """Remove markdown heading markers (# ## ###) and bold markers from text lines.
        Keeps the text content, just strips the formatting prefixes.
        """
        if not text:
            return text
        lines = text.split("\n")
        cleaned = []
        for line in lines:
            # strip heading markers: ### title → title
            stripped = re.sub(r'^#{1,6}\s+', '', line)
            # strip leading bold markers that act as pseudo-headers: **title** → title
            # but keep inline bold that's part of a sentence
            stripped = re.sub(r'^\*\*(.+?)\*\*\s*[：:—–-]?\s*', lambda m: m.group(1) + ' ', stripped)
            if stripped.strip():
                cleaned.append(stripped)
        return "\n".join(cleaned)

    def linkify_urls(self, text):
        """Convert plain URLs in text to Feishu markdown hyperlink format.
        https://example.com → [https://example.com](https://example.com)
        Only targets URLs wrapped in backticks or bare URLs.
        """
        if not text:
            return text
        # replace backtick-wrapped URLs: `https://...` → [text](url)
        def replace_backtick_url(m):
            url = m.group(1)
            # extract display text from URL (last meaningful path segment or domain)
            display = re.sub(r'^https?://', '', url)
            display = display.rstrip('/')
            # shorten: take domain + last path segment
            parts = display.split('/')
            if len(parts) > 2:
                display = parts[0] + '/' + parts[-1]
            return f"[{display}]({url})"
        text = re.sub(r'`(https?://[^\s`]+)`', replace_backtick_url, text)
        # replace bare URLs not already in a link: https://... → [url](url)
        text = re.sub(r'(?<!\[(?:.*?\]\())(https?://[^\s<>\)]+)', lambda m: f"[{m.group(0)}]({m.group(0)})", text)
        return text

    def build_execution_detail(self, transcript_data):
        """Build execution detail: files+commands+decisions, one line per item"""
        sections = []

        # --- files section (edit + new + read, one line per file) ---
        file_lines = []
        files_mod = transcript_data.get("files_modified", {})
        for fname, info in files_mod.items():
            added = info["lines_added"]
            removed = info["lines_removed"]
            edit_count = info["edit_count"]
            if removed > 0:
                change_str = f"+{added}/-{removed}"
            else:
                change_str = f"+{added}"
            file_lines.append(f"<font color='green'>编辑</font> `{fname}` {change_str} {edit_count}处")

        written = transcript_data.get("files_written", [])
        written_set = set(written) - set(files_mod.keys())
        for fname in written_set:
            file_lines.append(f"<font color='green'>新建</font> `{fname}`")

        files_read = transcript_data.get("files_read", [])
        for fname in files_read[:8]:
            file_lines.append(f"<font color='grey'>读取</font> `{fname}`")
        if len(files_read) > 8:
            file_lines.append(f"<font color='grey'>读取</font> 等{len(files_read)}个文件")

        if file_lines:
            sections.append("<font color='wathet'>**文件**</font>\n" + "\n".join(file_lines))

        # --- commands section (show original command, verb translated) ---
        commands = transcript_data.get("commands_run", [])
        if commands:
            unique_cmds = list(dict.fromkeys(commands))[:5]
            cmd_lines = []
            for i, cmd in enumerate(unique_cmds):
                cmd_lines.append(f"{i + 1}. `{cmd}`")
            sections.append("<font color='orange'>**执行**</font>\n" + "\n".join(cmd_lines))

        # --- decisions section (strip markdown headers, linkify URLs) ---
        decisions = transcript_data.get("decisions", [])
        if decisions:
            significant = sorted(decisions, key=len, reverse=True)[:3]
            decision_lines = []
            for i, d in enumerate(significant):
                # strip markdown headings and bold pseudo-headers
                d = self.strip_markdown_headers(d)
                # linkify any URLs
                d = self.linkify_urls(d)
                # take first line only
                first = d.split("\n")[0].strip()
                if not first:
                    first = d[:60]
                elif len(first) > 60:
                    first = first[:57] + "..."
                decision_lines.append(f"{i + 1}. {first}")
            sections.append("<font color='purple'>**决策**</font>\n" + "\n".join(decision_lines))

        return sections

    def normalize_model_name(self, model):
        """Normalize model name by stripping date suffix, variant markers, and -latest."""
        if not model:
            return model
        # strip -latest suffix
        stripped = re.sub(r'-latest$', '', model)
        # strip @version suffix (used by some providers)
        stripped = re.sub(r'@[\w-]+$', '', stripped)
        # strip trailing date suffix: -YYYYMMDD or -YYYY-MM-DD
        stripped = re.sub(r'-(\d{4})(\d{2})(\d{2})$', '', stripped)
        stripped = re.sub(r'-(\d{4})-(\d{2})-(\d{2})$', '', stripped)
        return stripped

    def estimate_cost(self, total_input, total_output, model, cache_read=0, cache_creation=0):
        """Estimate cost from token usage based on model pricing (USD per million tokens)"""
        if not model:
            return "unknown"
        pricing = {
            # --- Anthropic Claude ---
            "claude-opus-4-7": {"input": 15, "output": 75, "cache_read": 1.875, "cache_creation": 18.75},
            "claude-sonnet-4-6": {"input": 3, "output": 15, "cache_read": 0.30, "cache_creation": 3.75},
            "claude-haiku-4-5": {"input": 0.80, "output": 4, "cache_read": 0.08, "cache_creation": 0.80},

            # --- OpenAI GPT ---
            "gpt-4.5-preview": {"input": 75, "output": 150, "cache_read": 37.50, "cache_creation": 75},
            "gpt-4o": {"input": 5, "output": 15, "cache_read": 1.25, "cache_creation": 2.50},
            "gpt-4o-mini": {"input": 0.15, "output": 0.60, "cache_read": 0.075, "cache_creation": 0.15},
            "o1": {"input": 15, "output": 60, "cache_read": 7.50, "cache_creation": 15},
            "o1-mini": {"input": 1.10, "output": 4.40, "cache_read": 0.55, "cache_creation": 1.10},
            "o3-mini": {"input": 1.10, "output": 4.40, "cache_read": 0.55, "cache_creation": 1.10},

            # --- DeepSeek ---
            "deepseek-chat": {"input": 0.14, "output": 0.28, "cache_read": 0.014, "cache_creation": 0.14},
            "deepseek-reasoner": {"input": 0.55, "output": 2.19, "cache_read": 0.14, "cache_creation": 0.55},

            # --- Moonshot Kimi ---
            "kimi-moonshot-v1-8k": {"input": 1.67, "output": 1.67, "cache_read": 0.17, "cache_creation": 1.67},
            "kimi-moonshot-v1-32k": {"input": 1.67, "output": 1.67, "cache_read": 0.17, "cache_creation": 1.67},
            "kimi-moonshot-v1-128k": {"input": 1.67, "output": 1.67, "cache_read": 0.17, "cache_creation": 1.67},
            "kimi-latest": {"input": 1.67, "output": 1.67, "cache_read": 0.17, "cache_creation": 1.67},
            "kimi-for-coding": {"input": 2, "output": 2, "cache_read": 0.20, "cache_creation": 2.00},
            "kimi-k1": {"input": 1.67, "output": 1.67, "cache_read": 0.17, "cache_creation": 1.67},
            "kimi-k1.5": {"input": 1.67, "output": 1.67, "cache_read": 0.17, "cache_creation": 1.67},

            # --- Alibaba Qwen ---
            "qwen-max": {"input": 2.40, "output": 4.80, "cache_read": 1.20, "cache_creation": 2.40},
            "qwen-plus": {"input": 0.80, "output": 2.00, "cache_read": 0.40, "cache_creation": 0.80},
            "qwen-turbo": {"input": 0.30, "output": 0.60, "cache_read": 0.15, "cache_creation": 0.30},
            "qwen-coder-plus": {"input": 0.70, "output": 1.40, "cache_read": 0.35, "cache_creation": 0.70},
            "qwen-coder-turbo": {"input": 0.20, "output": 0.60, "cache_read": 0.10, "cache_creation": 0.20},

            # --- Zhipu GLM ---
            "glm-5.1": {"input": 15, "output": 75, "cache_read": 1.875, "cache_creation": 18.75},
            "glm-4": {"input": 1.00, "output": 1.00, "cache_read": 0.10, "cache_creation": 1.00},
            "glm-4-plus": {"input": 2.00, "output": 2.00, "cache_read": 0.20, "cache_creation": 2.00},
            "glm-4-flash": {"input": 0.05, "output": 0.05, "cache_read": 0.005, "cache_creation": 0.05},
            "glm-4-air": {"input": 0.10, "output": 0.10, "cache_read": 0.01, "cache_creation": 0.10},

            # --- MiniMax ---
            "abab6.5": {"input": 2.40, "output": 2.40, "cache_read": 0.24, "cache_creation": 2.40},
            "abab6.5s": {"input": 0.80, "output": 0.80, "cache_read": 0.08, "cache_creation": 0.80},
            "minimax-text-01": {"input": 0.13, "output": 0.13, "cache_read": 0.013, "cache_creation": 0.13},

            # --- Xiaomi ---
            "xiaomi-mi": {"input": 2.00, "output": 2.00, "cache_read": 0.20, "cache_creation": 2.00},
        }
        normalized = self.normalize_model_name(model)
        mp = pricing.get(normalized)
        if not mp:
            # sort keys by length descending for more specific prefix matches first
            sorted_keys = sorted(pricing.keys(), key=len, reverse=True)
            for key in sorted_keys:
                if normalized.startswith(key) or key.startswith(normalized):
                    mp = pricing[key]
                    break
        if not mp:
            # unknown model: return token counts as fallback
            total = total_input + total_output + cache_read + cache_creation
            if total > 0:
                parts = []
                if total_input > 0:
                    parts.append(f"in:{self._format_token_count(total_input)}")
                if total_output > 0:
                    parts.append(f"out:{self._format_token_count(total_output)}")
                if cache_read > 0:
                    parts.append(f"cr:{self._format_token_count(cache_read)}")
                if cache_creation > 0:
                    parts.append(f"cc:{self._format_token_count(cache_creation)}")
                return " / ".join(parts) if parts else "unknown"
            return "unknown"
        cost = (
            total_input / 1_000_000 * mp["input"]
            + total_output / 1_000_000 * mp["output"]
            + cache_read / 1_000_000 * mp["cache_read"]
            + cache_creation / 1_000_000 * mp["cache_creation"]
        )
        if cost < 0.01:
            return f"${cost:.4f}"
        elif cost < 1:
            return f"${cost:.2f}"
        else:
            return f"${cost:.1f}"

    def _format_token_count(self, tokens):
        """Format token count for display"""
        if tokens >= 1_000_000:
            return f"{tokens / 1_000_000:.1f}M"
        elif tokens >= 1_000:
            return f"{tokens / 1_000:.0f}K"
        else:
            return str(tokens)

    def format_cost_value(self, cost_val):
        """Format a cost value (float in USD) for display"""
        if cost_val is None:
            return "unknown"
        if isinstance(cost_val, str):
            return cost_val
        if cost_val < 0.01:
            return f"${cost_val:.4f}"
        elif cost_val < 1:
            return f"${cost_val:.2f}"
        else:
            return f"${cost_val:.1f}"

    def format_context_pct(self, max_input_tokens, model):
        """Format context window usage as percentage. Returns 'unknown' for unmatched models."""
        if not max_input_tokens:
            return None
        context_windows = {
            # --- Anthropic Claude ---
            "claude-opus-4-7": 1_000_000,
            "claude-sonnet-4-6": 200_000,
            "claude-haiku-4-5": 200_000,

            # --- OpenAI GPT ---
            "gpt-4.5-preview": 128_000,
            "gpt-4o": 128_000,
            "gpt-4o-mini": 128_000,
            "o1": 200_000,
            "o1-mini": 128_000,
            "o3-mini": 200_000,

            # --- DeepSeek ---
            "deepseek-chat": 64_000,
            "deepseek-reasoner": 64_000,

            # --- Moonshot Kimi ---
            "kimi-moonshot-v1-8k": 8_192,
            "kimi-moonshot-v1-32k": 32_768,
            "kimi-moonshot-v1-128k": 131_072,
            "kimi-latest": 256_000,
            "kimi-for-coding": 256_000,
            "kimi-k1": 131_072,
            "kimi-k1.5": 256_000,

            # --- Alibaba Qwen ---
            "qwen-max": 32_768,
            "qwen-plus": 131_072,
            "qwen-turbo": 131_072,
            "qwen-coder-plus": 131_072,
            "qwen-coder-turbo": 131_072,

            # --- Zhipu GLM ---
            "glm-5.1": 1_000_000,
            "glm-4": 131_072,
            "glm-4-plus": 131_072,
            "glm-4-flash": 131_072,
            "glm-4-air": 131_072,

            # --- MiniMax ---
            "abab6.5": 8_192,
            "abab6.5s": 8_192,
            "minimax-text-01": 1_000_000,

            # --- Xiaomi ---
            "xiaomi-mi": 128_000,
        }
        normalized = self.normalize_model_name(model) if model else None
        ctx_size = None
        if normalized:
            ctx_size = context_windows.get(normalized)
            # fallback: sort keys by length descending for more specific prefix matches first
            if not ctx_size:
                sorted_keys = sorted(context_windows.keys(), key=len, reverse=True)
                for key in sorted_keys:
                    if normalized.startswith(key) or key.startswith(normalized):
                        ctx_size = context_windows[key]
                        break
        if not ctx_size:
            # unknown model: show raw token count instead of "unknown"
            return f"{self._format_token_count(max_input_tokens)} used"
        pct = (max_input_tokens / ctx_size) * 100
        return f"{self._format_token_count(max_input_tokens)}/{self._format_token_count(ctx_size)} ({pct:.1f}%)"

    def build_feishu_card(self, workspace, notification_type, content_lines,
                          task_understanding=None, execution_detail=None,
                          cost=None, cumulative_cost=None, context_pct=None,
                          suggestion=None, prompt_summary=None, model=None,
                          duration=None, seq=None):
        """Build Feishu interactive card (schema 2.0) with hr-separated sections and badge row"""
        color_map = {
            "success": "green",
            "warning": "yellow",
            "urgent": "red",
            "info": "blue",
        }
        template = color_map.get(notification_type, "blue")

        bg_map = {
            "success": "green-300",
            "warning": "orange-300",
            "urgent": "red-300",
            "info": "blue-300",
        }
        bg_style = bg_map.get(notification_type, "blue-300")

        label_map = {
            "success": "已完成",
            "warning": "需操作",
            "urgent": "需授权",
            "info": "通知",
        }
        status_label = label_map.get(notification_type, "通知")

        # --- main content: task info ---
        main_md = "\n".join(content_lines)
        if task_understanding:
            # strip markdown headers from task understanding, linkify URLs
            clean_understanding = self.strip_markdown_headers(task_understanding)
            clean_understanding = self.linkify_urls(clean_understanding)
            # take first meaningful line (thinking can be long)
            first_line = clean_understanding.split("\n")[0].strip()
            if len(first_line) > 80:
                first_line = first_line[:77] + "..."
            main_md += f"\n<br><br>\n>**任务理解**\n- {first_line}"
        elif prompt_summary:
            main_md += f"\n<br><br>\n>**任务摘要**\n- `{prompt_summary}`"

        body_elements = [
            {
                "tag": "markdown",
                "content": main_md,
                "text_align": "left",
                "text_size": "normal_v2",
                "margin": "0px 0px 0px 0px",
            },
        ]

        # --- execution detail sections (each section separated by hr) ---
        if execution_detail:
            for section in execution_detail:
                body_elements.append({"tag": "hr", "margin": "0px 0px 0px 0px"})
                body_elements.append({
                    "tag": "markdown",
                    "content": ">" + section,
                    "text_align": "left",
                    "text_size": "normal_v2",
                    "margin": "0px 0px 0px 0px",
                })

        # --- suggestion (same block format as task understanding) ---
        if suggestion:
            body_elements.append({"tag": "hr", "margin": "0px 0px 0px 0px"})
            body_elements.append({
                "tag": "markdown",
                "content": f">**建议**\n{suggestion}",
                "text_align": "left",
                "text_size": "normal_v2",
                "margin": "0px 0px 0px 0px",
            })

        # --- badge row (column_set) ---
        # new layout: status#seq | duration | model | cost/cumulative | context | timestamp
        # no label prefixes (model:, cost:, context: removed)

        # 1. status badge with seq
        status_text = status_label
        if seq:
            status_text = f"{status_label} #{seq}"
        badge_columns = [
            {
                "tag": "column",
                "width": "auto",
                "background_style": bg_style,
                "elements": [
                    {
                        "tag": "markdown",
                        "content": f"<font color='white'>{status_text}</font>",
                        "text_align": "center",
                        "text_size": "notation",
                        "margin": "0px 0px 0px 0px",
                    }
                ],
                "padding": "0px 8px 0px 8px",
                "direction": "horizontal",
                "horizontal_spacing": "8px",
                "vertical_spacing": "0px",
                "horizontal_align": "left",
                "vertical_align": "top",
                "margin": "0px 0px 0px 0px",
            },
        ]

        # 2. duration badge
        if duration:
            badge_columns.append({
                "tag": "column",
                "width": "auto",
                "elements": [
                    {
                        "tag": "markdown",
                        "content": f"<font color='grey'>{duration}</font>",
                        "text_align": "center",
                        "text_size": "notation",
                        "margin": "0px 0px 0px 0px",
                    }
                ],
                "padding": "0px 8px 0px 8px",
                "horizontal_spacing": "8px",
                "vertical_spacing": "0px",
                "horizontal_align": "left",
                "vertical_align": "top",
                "margin": "0px 0px 0px 0px",
            })

        # 3. model badge (normalized, no "model:" prefix)
        display_model = self.normalize_model_name(model) if model else None
        badge_columns.append({
            "tag": "column",
            "width": "auto",
            "elements": [
                {
                    "tag": "markdown",
                    "content": f"<font color='grey'>{display_model}</font>" if display_model else "<font color='grey'>---</font>",
                    "text_align": "center",
                    "text_size": "notation",
                    "margin": "0px 0px 0px 0px",
                }
            ],
            "padding": "0px 8px 0px 8px",
            "horizontal_spacing": "8px",
            "vertical_spacing": "0px",
            "horizontal_align": "left",
            "vertical_align": "top",
            "margin": "0px 0px 0px 0px",
        })

        # 4. cost badge (per-round/cumulative, no "cost:" prefix)
        # color based on per-round cost: green < $1, orange $1-5, red > $5
        cost_color = "grey"
        cost_display = "---"
        if cost and cost != "unknown":
            try:
                cost_val = float(cost.replace("$", ""))
                if cost_val < 1:
                    cost_color = "green"
                elif cost_val < 5:
                    cost_color = "orange"
                else:
                    cost_color = "red"
            except ValueError:
                cost_color = "grey"
            if cumulative_cost and cumulative_cost != "unknown":
                cost_display = f"{cost}/{cumulative_cost}"
            else:
                cost_display = cost
        elif cost == "unknown":
            cost_display = "unknown"

        badge_columns.append({
            "tag": "column",
            "width": "auto",
            "elements": [
                {
                    "tag": "markdown",
                    "content": f"<font color='{cost_color}'>{cost_display}</font>",
                    "text_align": "center",
                    "text_size": "notation",
                    "margin": "0px 0px 0px 0px",
                }
            ],
            "padding": "0px 8px 0px 8px",
            "horizontal_spacing": "8px",
            "vertical_spacing": "0px",
            "horizontal_align": "left",
            "vertical_align": "top",
            "margin": "0px 0px 0px 0px",
        })

        # 5. context badge (no "context:" prefix)
        ctx_color = "grey"
        ctx_display = "---"
        if context_pct:
            ctx_pct_val = 0
            if context_pct != "unknown":
                pct_match = re.search(r'([\d.]+)%', context_pct)
                if pct_match:
                    ctx_pct_val = float(pct_match.group(1))
                if ctx_pct_val < 30:
                    ctx_color = "green"
                elif ctx_pct_val < 70:
                    ctx_color = "orange"
                else:
                    ctx_color = "red"
            ctx_display = context_pct

        badge_columns.append({
            "tag": "column",
            "width": "auto",
            "elements": [
                {
                    "tag": "markdown",
                    "content": f"<font color='{ctx_color}'>{ctx_display}</font>",
                    "text_align": "center",
                    "text_size": "notation",
                    "margin": "0px 0px 0px 0px",
                }
            ],
            "padding": "0px 8px 0px 8px",
            "horizontal_spacing": "8px",
            "vertical_spacing": "0px",
            "horizontal_align": "left",
            "vertical_align": "top",
            "margin": "0px 0px 0px 0px",
        })

        # 6. timestamp badge
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        badge_columns.append({
            "tag": "column",
            "width": "auto",
            "elements": [
                {
                    "tag": "markdown",
                    "content": f"<font color='grey'>{current_time}</font>",
                    "text_align": "center",
                    "text_size": "notation",
                    "margin": "0px 0px 0px 0px",
                }
            ],
            "padding": "0px 8px 0px 8px",
            "direction": "vertical",
            "horizontal_spacing": "8px",
            "vertical_spacing": "8px",
            "horizontal_align": "left",
            "vertical_align": "top",
            "margin": "0px 0px 0px 0px",
        })

        body_elements.append({"tag": "hr", "margin": "0px 0px 0px 0px"})
        body_elements.append({
            "tag": "column_set",
            "horizontal_spacing": "8px",
            "horizontal_align": "left",
            "columns": badge_columns,
            "margin": "0px 0px 0px 0px",
        })

        card = {
            "schema": "2.0",
            "config": {
                "update_multi": True,
                "style": {
                    "text_size": {
                        "normal_v2": {
                            "default": "normal",
                            "pc": "normal",
                            "mobile": "heading",
                        }
                    }
                },
            },
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": workspace,
                },
                "template": template,
            },
            "body": {
                "direction": "vertical",
                "padding": "12px 12px 12px 12px",
                "elements": body_elements,
            },
        }

        # log the card content for debuggability
        logging.info(f"[CARD] task_understanding={task_understanding[:80] if task_understanding else 'None'}, cost={cost}/{cumulative_cost}, model={model}, ctx={context_pct}")

        return card

    def get_tenant_access_token(self):
        """Get tenant_access_token via Feishu App API, with local caching"""
        if self._tenant_token and time.time() < self._token_expire_at:
            return self._tenant_token

        cfg = self.config
        url = f"{FEISHU_APP_API_BASE}/auth/v3/tenant_access_token/internal"
        payload = {
            "app_id": cfg["app_id"],
            "app_secret": cfg["app_secret"],
        }

        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data,
                headers={"Content-Type": "application/json"},
            )
            response = urllib.request.urlopen(req, timeout=10)
            result = json.loads(response.read().decode("utf-8"))

            if result.get("code") != 0:
                logging.error(
                    f"Failed to get tenant_access_token: code={result.get('code')}, msg={result.get('msg')}"
                )
                return None

            self._tenant_token = result.get("tenant_access_token")
            expire = result.get("expire", 7200)
            # Refresh 5 minutes before actual expiry
            self._token_expire_at = time.time() + expire - 300

            logging.info("Tenant access token refreshed successfully")
            return self._tenant_token

        except urllib.error.URLError as e:
            logging.error(f"Failed to request tenant_access_token: {e}")
            return None
        except Exception as e:
            logging.error(f"Error getting tenant_access_token: {e}")
            return None

    def send_via_app_mode(self, card):
        """Send notification via Feishu IM API (App mode). Returns (success, message_id)."""
        token = self.get_tenant_access_token()
        if not token:
            logging.error("Cannot send notification: no tenant_access_token")
            return (False, None)

        cfg = self.config
        receive_id_type = cfg.get("receive_id_type", "open_id")
        url = f"{FEISHU_APP_API_BASE}/im/v1/messages?receive_id_type={receive_id_type}"

        content_str = json.dumps(card, ensure_ascii=False)
        payload = {
            "receive_id": cfg["receive_id"],
            "msg_type": "interactive",
            "content": content_str,
        }

        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Authorization": f"Bearer {token}",
                },
            )
            response = urllib.request.urlopen(req, timeout=10)
            result = json.loads(response.read().decode("utf-8"))

            if result.get("code") != 0:
                logging.error(
                    f"Feishu IM API error: code={result.get('code')}, msg={result.get('msg')}"
                )
                return (False, None)
            else:
                message_id = result.get("data", {}).get("message_id")
                logging.info(f"Feishu card delivered to {cfg['receive_id']} (App mode), msg_id={message_id}")
                return (True, message_id)

        except urllib.error.URLError as e:
            logging.error(f"Feishu IM API request failed: {e}")
            self._tenant_token = None
            self._token_expire_at = 0
            return (False, None)
        except Exception as e:
            logging.error(f"Error sending notification via App mode: {e}")
            return (False, None)

    def update_card(self, message_id, card):
        """Update an existing Feishu card by message_id. Returns bool."""
        token = self.get_tenant_access_token()
        if not token or not message_id:
            return False

        url = f"{FEISHU_APP_API_BASE}/im/v1/messages/{message_id}"
        content_str = json.dumps(card, ensure_ascii=False)
        payload = {
            "msg_type": "interactive",
            "content": content_str,
        }

        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Authorization": f"Bearer {token}",
                },
                method="PATCH",
            )
            response = urllib.request.urlopen(req, timeout=10)
            result = json.loads(response.read().decode("utf-8"))

            if result.get("code") != 0:
                logging.error(f"Feishu card update failed: code={result.get('code')}, msg={result.get('msg')}")
                return False
            else:
                logging.info(f"Feishu card updated, msg_id={message_id}")
                return True

        except Exception as e:
            logging.error(f"Error updating Feishu card: {e}")
            return False

    def send_via_webhook_mode(self, card):
        """Send notification via Feishu custom bot webhook"""
        cfg = self.config
        webhook_url = cfg["webhook_url"]
        payload = {"msg_type": "interactive", "card": card}

        # Add signature if secret is configured
        webhook_secret = cfg.get("webhook_secret", "")
        if webhook_secret:
            timestamp, sign = self.generate_sign(webhook_secret)
            payload["timestamp"] = timestamp
            payload["sign"] = sign

        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                webhook_url,
                data=data,
                headers={"Content-Type": "application/json"},
            )
            response = urllib.request.urlopen(req, timeout=10)
            result = json.loads(response.read().decode("utf-8"))

            code = result.get("code", result.get("StatusCode", -1))
            if code != 0:
                logging.error(
                    f"Feishu webhook API error: code={code}, msg={result.get('msg', 'unknown')}"
                )
                return False
            else:
                logging.info(f"Feishu card delivered (webhook mode)")
                return True

        except urllib.error.URLError as e:
            logging.error(f"Feishu webhook request failed: {e}")
            return False
        except Exception as e:
            logging.error(f"Error sending notification via webhook: {e}")
            return False

    def save_summary_queue(self, session_id, workspace, seq, message_id,
                           duration, cost, cumulative_cost=None,
                           context_pct=None, transcript_data=None):
        """Save raw transcript data to summary queue for later AI processing"""
        queue_path = os.path.join(self.project_dir, "db", "summary_queue.json")
        queue = []
        if os.path.exists(queue_path):
            try:
                with open(queue_path, "r", encoding="utf-8") as f:
                    queue = json.load(f)
            except Exception:
                queue = []

        queue.append({
            "session_id": session_id,
            "workspace": workspace,
            "seq": seq,
            "message_id": message_id,
            "duration": duration,
            "cost": cost,
            "cumulative_cost": cumulative_cost,
            "context_pct": context_pct,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "raw_data": {
                "task_understanding": transcript_data.get("task_understanding", ""),
                "commands_run": transcript_data.get("commands_run", []),
                "decisions": transcript_data.get("decisions", []),
                "last_suggestion": transcript_data.get("last_suggestion", ""),
                "files_modified": transcript_data.get("files_modified", {}),
                "files_written": transcript_data.get("files_written", []),
            },
            "status": "pending",
        })

        # keep only last 10 pending items
        queue = [item for item in queue if item.get("status") != "done"][-10:]

        with open(queue_path, "w", encoding="utf-8") as f:
            json.dump(queue, f, ensure_ascii=False, indent=2)

    def send_notification(self, workspace, notification_type, content_lines,
                          task_understanding=None, execution_detail=None,
                          cost=None, cumulative_cost=None, context_pct=None,
                          suggestion=None, prompt_summary=None, model=None,
                          duration=None, seq=None):
        """Send notification via Feishu. Returns (success, message_id)."""
        if not self.send_mode:
            logging.warning(
                "Feishu notification not configured. "
                "Set webhook_url or (app_id + app_secret + receive_id) in config/env."
            )
            return (False, None)

        card = self.build_feishu_card(
            workspace, notification_type, content_lines,
            task_understanding, execution_detail,
            cost, cumulative_cost, context_pct, suggestion, prompt_summary,
            model, duration, seq
        )

        if self.send_mode == "app":
            return self.send_via_app_mode(card)
        elif self.send_mode == "webhook":
            result = self.send_via_webhook_mode(card)
            return (result, None)
        return (False, None)


def sanitize_surrogates(obj):
    """Remove lone surrogate characters that crash UTF-8 encoding on Windows"""
    if isinstance(obj, str):
        return obj.encode("utf-8", errors="surrogatepass").decode("utf-8", errors="replace")
    elif isinstance(obj, dict):
        return {sanitize_surrogates(k): sanitize_surrogates(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [sanitize_surrogates(item) for item in obj]
    return obj


def validate_input_data(data, expected_event_name):
    """Validate input data matches design specification"""
    required_fields = {
        "UserPromptSubmit": ["session_id", "prompt", "cwd", "hook_event_name"],
        "Stop": ["session_id", "hook_event_name"],
        "Notification": ["session_id", "message", "hook_event_name"],
    }

    if expected_event_name not in required_fields:
        raise ValueError(f"Unknown event type: {expected_event_name}")

    if data.get("hook_event_name") != expected_event_name:
        raise ValueError(
            f"Event name mismatch: expected {expected_event_name}, got {data.get('hook_event_name')}"
        )

    missing_fields = []
    for field in required_fields[expected_event_name]:
        if field not in data or data[field] is None:
            missing_fields.append(field)

    if missing_fields:
        raise ValueError(
            f"Missing required fields for {expected_event_name}: {missing_fields}"
        )

    return True


def main():
    """Main entry point - read JSON from stdin and process event"""
    try:
        if len(sys.argv) < 2:
            print("ok")
            return

        expected_event_name = sys.argv[1]

        # UpdateCard: external command to update a card with AI summaries
        # Usage: python ccfeishunotify.py UpdateCard <JSON with message_id + summaries>
        if expected_event_name == "UpdateCard":
            input_data = sys.stdin.buffer.read().decode("utf-8", errors="replace").strip()
            if not input_data:
                logging.error("No input data for UpdateCard")
                sys.exit(1)
            data = json.loads(input_data)
            data = sanitize_surrogates(data)

            tracker = ClaudePromptTracker()
            message_id = data.get("message_id")
            if not message_id:
                logging.error("UpdateCard requires message_id")
                sys.exit(1)

            card = tracker.build_feishu_card(
                workspace=data.get("workspace", "unknown"),
                notification_type=data.get("notification_type", "success"),
                content_lines=data.get("content_lines", []),
                task_understanding=data.get("task_understanding"),
                execution_detail=data.get("execution_detail"),
                cost=data.get("cost"),
                cumulative_cost=data.get("cumulative_cost"),
                context_pct=data.get("context_pct"),
                suggestion=data.get("suggestion"),
                model=data.get("model"),
                duration=data.get("duration"),
                seq=data.get("seq"),
            )

            success = tracker.update_card(message_id, card)
            if success:
                # mark queue item as done
                queue_path = os.path.join(tracker.project_dir, "db", "summary_queue.json")
                if os.path.exists(queue_path):
                    try:
                        with open(queue_path, "r", encoding="utf-8") as f:
                            queue = json.load(f)
                        for item in queue:
                            if item.get("message_id") == message_id:
                                item["status"] = "done"
                        with open(queue_path, "w", encoding="utf-8") as f:
                            json.dump(queue, f, ensure_ascii=False, indent=2)
                    except Exception as e:
                        logging.warning(f"Failed to update queue: {e}")
                print("ok")
            else:
                sys.exit(1)
            return

        valid_events = ["UserPromptSubmit", "Stop", "Notification"]

        if expected_event_name not in valid_events:
            logging.error(f"Invalid hook type: {expected_event_name}")
            logging.error(f"Valid hook types: {', '.join(valid_events)}")
            sys.exit(1)

        input_data = sys.stdin.buffer.read().decode("utf-8", errors="replace").strip()
        if not input_data:
            logging.warning("No input data received")
            return

        data = json.loads(input_data)
        data = sanitize_surrogates(data)
        validate_input_data(data, expected_event_name)

        tracker = ClaudePromptTracker()

        if expected_event_name == "UserPromptSubmit":
            tracker.handle_user_prompt_submit(data)
        elif expected_event_name == "Stop":
            tracker.handle_stop(data)
        elif expected_event_name == "Notification":
            tracker.handle_notification(data)

    except json.JSONDecodeError as e:
        logging.error(f"JSON decode error: {e}")
        sys.exit(1)
    except ValueError as e:
        logging.error(f"Validation error: {e}")
        sys.exit(1)
    except Exception as e:
        logging.error(f"Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()