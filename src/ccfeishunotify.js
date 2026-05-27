#!/usr/bin/env node
/**
 * Claude Code Feishu Notify
 * Send Claude Code notifications to Feishu (Lark) via App API or custom bot webhook.
 * Based on CCNotify (https://github.com/dazuiba/CCNotify)
 *
 * Supports two modes:
 * 1. App mode: uses app_id + app_secret to get tenant_access_token, sends via IM API
 * 2. Webhook mode: uses custom bot webhook URL directly (simpler setup)
 *
 * Zero external dependencies — uses Node.js built-in modules only.
 * State tracking via JSON file (replaces SQLite for portability).
 */

const fs = require("fs")
const path = require("path")
const https = require("https")
const http = require("http")
const crypto = require("crypto")
const os = require("os")

const FEISHU_APP_API_BASE = "https://open.feishu.cn/open-apis"

const ENV_MAP = {
  webhook_url: "FEISHU_WEBHOOK_URL",
  webhook_secret: "FEISHU_WEBHOOK_SECRET",
  app_id: "FEISHU_APP_ID",
  app_secret: "FEISHU_APP_SECRET",
  receive_id: "FEISHU_RECEIVE_ID",
  receive_id_type: "FEISHU_RECEIVE_ID_TYPE",
  ai_base_url: "ANTHROPIC_BASE_URL",
  ai_api_key: "ANTHROPIC_API_KEY",
}

// simple English→Chinese keyword map for task understanding
const EN_ZH_MAP = {
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

// --- HTTP helper (async, supports both http and https) ---
function http_request(url_str, method, headers, body_buf, timeout_ms) {
  return new Promise((resolve, reject) => {
    const parsed = new URL(url_str)
    const mod = parsed.protocol === "https:" ? https : http
    const opts = {
      hostname: parsed.hostname,
      port: parsed.port || (parsed.protocol === "https:" ? 443 : 80),
      path: parsed.pathname + parsed.search,
      method: method || "POST",
      headers: headers || {},
      timeout: timeout_ms || 10000,
    }
    const req = mod.request(opts, (res) => {
      const chunks = []
      res.on("data", (chunk) => chunks.push(chunk))
      res.on("end", () => {
        const raw = Buffer.concat(chunks).toString("utf-8")
        let body
        try { body = JSON.parse(raw) } catch { body = raw }
        resolve({ statusCode: res.statusCode, headers: res.headers, body })
      })
    })
    req.on("error", (e) => reject(e))
    req.on("timeout", () => { req.destroy(); reject(new Error("Request timeout")) })
    if (body_buf) req.write(body_buf)
    req.end()
  })
}

function now_iso() {
  // UTC+8 timestamp
  const d = new Date(Date.now() + 8 * 3600000)
  return d.toISOString().replace("T", " ").replace(/\.\d+Z$/, "")
}

class ClaudePromptTracker {
  constructor() {
    const script_dir = path.dirname(fs.realpathSync(__filename))
    // if script lives in a src/ subdirectory, project root is parent
    const project_dir = path.basename(script_dir) === "src"
      ? path.dirname(script_dir)
      : script_dir
    this.project_dir = project_dir
    this.state_path = path.join(project_dir, "db", "ccfeishunotify_state.json")
    this.log_path = path.join(project_dir, "logs", "ccfeishunotify.log")
    // ensure subdirectories exist
    for (const subdir of ["db", "logs", "configs"]) {
      fs.mkdirSync(path.join(project_dir, subdir), { recursive: true })
    }
    // load config and determine mode
    this.config = this.load_config()
    this.send_mode = this.determine_send_mode()
    // token cache for app mode
    this._tenant_token = null
    this._token_expire_at = 0
    // cc-switch: read ANTHROPIC_MODEL env var for model awareness
    this.cc_switch_model = process.env.ANTHROPIC_MODEL || null
  }

  // --- Config ---
  load_config() {
    const config = {
      webhook_url: "",
      webhook_secret: "",
      app_id: "",
      app_secret: "",
      receive_id: "",
      receive_id_type: "",
      ai_base_url: "",
      ai_api_key: "",
      ai_model: "claude-sonnet-4-6",
    }
    // priority 2: config file (lowest)
    const config_path = path.join(this.project_dir, "configs", "feishu_config.json")
    if (fs.existsSync(config_path)) {
      try {
        const file_config = JSON.parse(fs.readFileSync(config_path, "utf-8"))
        for (const key in config) {
          if (file_config[key]) config[key] = file_config[key]
        }
      } catch (e) {
        this._log("error", `Error reading config file ${config_path}: ${e.message}`)
      }
    }
    // priority 1: environment variables (override config file)
    for (const key in ENV_MAP) {
      const val = process.env[ENV_MAP[key]]
      if (val) config[key] = val
    }
    // cc-switch compatibility: ANTHROPIC_AUTH_TOKEN is used by most cc-switch providers
    if (!config.ai_api_key && process.env.ANTHROPIC_AUTH_TOKEN) {
      config.ai_api_key = process.env.ANTHROPIC_AUTH_TOKEN
    }
    // default receive_id_type
    if (!config.receive_id_type) config.receive_id_type = "open_id"
    return config
  }

  determine_send_mode() {
    if (this.config.webhook_url) return "webhook"
    if (this.config.app_id && this.config.app_secret && this.config.receive_id) return "app"
    return null
  }

  // --- Logging ---
  _log(level, msg) {
    const ts = now_iso()
    const line = `${ts} - ${level.toUpperCase()} - ${msg}\n`
    try {
      // simple daily rotation: check if log file date changed
      if (fs.existsSync(this.log_path)) {
        const stat = fs.statSync(this.log_path)
        const file_date = stat.mtime.toISOString().slice(0, 10)
        const today = new Date().toISOString().slice(0, 10)
        if (file_date !== today) {
          // rotate: rename old file with date suffix, keep 1 backup
          const backup = this.log_path + "." + file_date
          try { fs.unlinkSync(backup) } catch {}
          try { fs.renameSync(this.log_path, backup) } catch {}
        }
      }
      fs.appendFileSync(this.log_path, line, "utf-8")
    } catch {}
  }

  // --- State (JSON file-based) ---
  _load_state() {
    if (!fs.existsSync(this.state_path)) {
      return { prompts: [], last_id: 0 }
    }
    try {
      return JSON.parse(fs.readFileSync(this.state_path, "utf-8"))
    } catch {
      return { prompts: [], last_id: 0 }
    }
  }

  _save_state(state) {
    // prune: keep only last 500 records
    if (state.prompts.length > 500) {
      state.prompts = state.prompts.slice(-500)
    }
    const tmp = this.state_path + ".tmp"
    fs.writeFileSync(tmp, JSON.stringify(state, null, 2), "utf-8")
    try { fs.unlinkSync(this.state_path) } catch {}
    fs.renameSync(tmp, this.state_path)
  }

  // --- Handlers ---
  handle_user_prompt_submit(data) {
    const state = this._load_state()
    const session_id = data.session_id || ""
    const prompt = data.prompt || ""
    const cwd = data.cwd || ""
    // compute seq: count existing prompts for this session + 1
    const existing = state.prompts.filter((p) => p.session_id === session_id)
    const seq = existing.length > 0 ? Math.max(...existing.map((p) => p.seq || 0)) + 1 : 1
    const record = {
      id: ++state.last_id,
      session_id,
      created_at: now_iso(),
      prompt,
      cwd,
      seq,
      stoped_at: null,
      cumulative_cost: null,
      last_wait_user_at: null,
    }
    state.prompts.push(record)
    this._save_state(state)
    this._log("info", `Recorded prompt for session ${session_id}`)
  }

  async handle_stop(data) {
    const session_id = data.session_id || ""
    const cost_usd = data.costUSD

    this._log("info", `[STOP] raw data keys: ${Object.keys(data).join(",")}`)

    const state = this._load_state()
    // find latest open prompt for this session
    const record = state.prompts
      .filter((p) => p.session_id === session_id && !p.stoped_at)
      .sort((a, b) => (a.created_at > b.created_at ? -1 : 1))[0]

    if (!record) return

    record.stoped_at = now_iso()
    const seq = record.seq || 1
    const duration = this.calculate_duration(record.created_at, record.stoped_at)
    const cwd = record.cwd || ""
    const workspace = path.basename(cwd) || "unknown"

    // prefer transcript_path from hook data, fallback to computed path
    const transcript_path = data.transcript_path || (cwd ? this.find_transcript_path(session_id, cwd) : null)
    const transcript_data = transcript_path ? this.parse_transcript_summary(transcript_path) : null

    let task_understanding = ""
    let execution_detail = null
    let display_cost = null
    let display_cumulative_cost = null
    let context_pct = null
    let suggestion = "请检查结果，决定下一步操作"

    if (transcript_data) {
      const model = transcript_data.model || this.cc_switch_model

      // task understanding: combine prompt + thinking, AI-summarize into Chinese
      let understanding_source = ""
      if (record.prompt) understanding_source = `用户输入: ${record.prompt}`
      if (transcript_data.task_understanding) {
        const thinking_clean = this.strip_markdown_headers(transcript_data.task_understanding)
        const thinking_first = thinking_clean.split("\n")[0].trim().slice(0, 200)
        if (thinking_first) understanding_source += `\n模型理解: ${thinking_first}`
      }
      let task_ai_processed = false
      if (understanding_source) {
        const task_fallback = this.generate_task_summary_fallback(record.prompt)
        const result = await this.ai_summarize(understanding_source, "task", task_fallback)
        task_understanding = result
        // if result equals fallback, AI didn't actually process it
        task_ai_processed = result !== task_fallback && result.length > 0
      }

      // consolidate and AI-summarize execution steps
      const steps = transcript_data.steps || []
      if (steps.length) {
        const consolidated = this._consolidate_steps(steps)
        const step_summaries = await this.ai_summarize_steps(consolidated)
        transcript_data.consolidated_steps = consolidated
        transcript_data.step_summaries = step_summaries
      }

      execution_detail = this.build_execution_detail(transcript_data)
      context_pct = this.format_context_pct(transcript_data.max_input_tokens, model)

      // cost estimation
      let cumulative_cost_val = null
      if (cost_usd) {
        cumulative_cost_val = cost_usd
      } else {
        const estimated = this.estimate_cost(
          transcript_data.total_input_tokens,
          transcript_data.total_output_tokens,
          model,
          transcript_data.total_cache_read_tokens,
          transcript_data.total_cache_creation_tokens,
        )
        if (estimated !== "unknown") {
          cumulative_cost_val = parseFloat(estimated.replace("$", ""))
        }
      }

      // get previous cumulative cost for this session
      const prev_records = state.prompts
        .filter((p) => p.session_id === session_id && p.cumulative_cost != null && p.id !== record.id)
        .sort((a, b) => b.id - a.id)
      const prev_cumulative = prev_records.length > 0 ? prev_records[0].cumulative_cost : 0

      if (cumulative_cost_val != null) {
        const per_round_val = cumulative_cost_val - prev_cumulative
        display_cost = this.format_cost_value(per_round_val)
        display_cumulative_cost = this.format_cost_value(cumulative_cost_val)
        record.cumulative_cost = cumulative_cost_val
      }

      if (!cost_usd && !cumulative_cost_val) {
        display_cost = "unknown"
        display_cumulative_cost = "unknown"
      }

      suggestion = transcript_data.last_suggestion || suggestion

      // override suggestion with last consolidated step's text if it's more conclusive
      if (transcript_data.consolidated_steps && transcript_data.consolidated_steps.length) {
        const last_step = transcript_data.consolidated_steps[transcript_data.consolidated_steps.length - 1]
        if (last_step.text && last_step.text.length > 20) {
          // last step's text is the actual conclusion, not an intermediate description
          suggestion = last_step.text
        }
      }
    }

    // ending classification: determine label (行动建议/总结/结论/汇报)
    let ending_label = "结果汇总"
    if (suggestion && suggestion !== "请检查结果，决定下一步操作" && transcript_data) {
      const ending_fallback = `${this.classify_ending_label(suggestion)}：${suggestion.slice(0, 50)}`
      const ending_result = await this.ai_summarize(suggestion.slice(0, 300), "ending", ending_fallback)
      if (ending_result) {
        // parse format like "行动建议：xxx" or "总结：xxx"
        const valid_labels = ["行动建议", "总结", "结论", "汇报", "完成总结"]
        const matched = valid_labels.find((l) => ending_result.startsWith(l))
        if (matched) {
          ending_label = matched
          const rest = ending_result.slice(matched.length).replace(/^[：:]/, "").trim()
          if (rest) suggestion = rest
        } else {
          ending_label = this.classify_ending_label(suggestion)
        }
      }
    }

    this._save_state(state)

    const content_lines = [`第 \`#${seq}\` 轮任务已完成`]
    const [sent, message_id] = await this.send_notification({
      workspace,
      notification_type: "success",
      content_lines,
      task_understanding,
      task_ai_processed,
      execution_detail,
      cost: display_cost,
      cumulative_cost: display_cumulative_cost,
      context_pct,
      suggestion,
      ending_label,
      model: transcript_data ? (transcript_data.model || this.cc_switch_model) : this.cc_switch_model,
      duration,
      seq,
    })

    const status = sent ? "card sent" : "card send FAILED"
    this._log("info", `[${workspace}] job#${seq} done, duration=${duration}, cost=${display_cost}/${display_cumulative_cost}, msg_id=${message_id}, ${status}`)
    if (transcript_data && sent) {
      this.save_summary_queue(session_id, workspace, seq, message_id, duration, display_cost, display_cumulative_cost, context_pct, transcript_data)
    }
  }

  handle_notification(data) {
    const session_id = data.session_id || ""
    const message = data.message || ""
    const cwd = data.cwd || ""

    this._log("info", `[NOTIFICATION] session=${session_id}, message='${message}'`)

    const message_lower = message.toLowerCase()
    const workspace = path.basename(cwd) || "unknown"
    let should_update_db = false
    let should_notify = true
    let notification_type = "info"
    let label = ""
    let suggestion = ""

    if (message_lower.includes("waiting for your input") || message_lower.includes("waiting for input")) {
      label = "等待输入"
      should_update_db = true
      should_notify = false
      notification_type = "warning"
      suggestion = "请回到 Claude Code 查看并回复"
    } else if (message_lower.includes("permission")) {
      label = "需要授权"
      notification_type = "urgent"
      suggestion = "请尽快授权以继续任务"
    } else if (message_lower.includes("approval") || message_lower.includes("choose an option")) {
      label = "需要操作"
      notification_type = "warning"
      suggestion = "请尽快选择操作以继续任务"
    } else {
      label = "通知"
      suggestion = "请查看 Claude Code"
    }

    if (should_update_db) {
      const state = this._load_state()
      const rec = state.prompts
        .filter((p) => p.session_id === session_id)
        .sort((a, b) => (a.created_at > b.created_at ? -1 : 1))[0]
      if (rec) {
        rec.last_wait_user_at = now_iso()
        this._save_state(state)
        this._log("info", `Updated lastWaitUserAt for session ${session_id}`)
      }
    }

    if (should_notify) {
      const content_lines = [`**${label}**`, `提示: ${message}`]
      const result = this.send_notification({ workspace, notification_type, content_lines, suggestion })
      result.then(([sent]) => {
        const status = sent ? "card sent" : "card send FAILED"
        this._log("info", `[${workspace}] ${label}, ${status}`)
      }).catch((e) => {
        this._log("error", `[${workspace}] notification send failed: ${e.message}`)
      })
    } else {
      this._log("info", `[${workspace}] ${label} (suppressed, will notify on Stop)`)
    }
  }

  // --- Duration ---
  calculate_duration(start_time, end_time) {
    try {
      const start = new Date(start_time)
      const end = new Date(end_time)
      let total_seconds = Math.floor((end - start) / 1000)
      if (total_seconds < 0) total_seconds = 0

      if (total_seconds < 60) return `${total_seconds}s`
      if (total_seconds < 3600) {
        const m = Math.floor(total_seconds / 60)
        const s = total_seconds % 60
        return s > 0 ? `${m}m${s}s` : `${m}m`
      }
      const h = Math.floor(total_seconds / 3600)
      const m = Math.floor((total_seconds % 3600) / 60)
      return m > 0 ? `${h}h${m}m` : `${h}h`
    } catch (e) {
      this._log("error", `Error calculating duration: ${e.message}`)
      return "Unknown"
    }
  }

  // --- Sign ---
  generate_sign(secret) {
    const timestamp = String(Math.floor(Date.now() / 1000))
    const string_to_sign = `${timestamp}\n${secret}`
    const hash = crypto.createHash("sha256").update(string_to_sign, "utf-8").digest()
    const sign = hash.toString("base64")
    return { timestamp, sign }
  }

  // --- Transcript ---
  cwd_to_project_dir(cwd) {
    let result = cwd.replace(/:/g, "-").replace(/\\/g, "-").replace(/\//g, "-")
    // Claude Code uses lowercase drive letter on Windows
    if (result.length >= 2 && /[a-zA-Z]/.test(result[0]) && result[1] === "-") {
      result = result[0].toLowerCase() + result.slice(1)
    }
    return result
  }

  find_transcript_path(session_id, cwd) {
    if (!cwd) return null
    const project_dir = this.cwd_to_project_dir(cwd)
    const home = os.homedir()
    const tp = path.join(home, ".claude", "projects", project_dir, `${session_id}.jsonl`)
    return fs.existsSync(tp) ? tp : null
  }

  parse_transcript_summary(transcript_path) {
    let total_input_tokens = 0
    let total_output_tokens = 0
    let total_cache_read_tokens = 0
    let total_cache_creation_tokens = 0
    let max_input_tokens = 0
    let model = null
    const seen_msg_ids = new Set()

    // flat structures (backward compat for summary queue)
    let task_understanding = ""
    let first_thinking_found = false
    let last_suggestion = ""
    const files_modified = {}
    const files_written = []
    const files_read_set = new Set()
    const files_read = []
    const commands_run = []
    const decisions = []

    // chronological steps (new)
    const steps = []

    try {
      const lines = fs.readFileSync(transcript_path, "utf-8").split("\n")
      for (const line of lines) {
        let data
        try { data = JSON.parse(line) } catch { continue }

        if (data.type === "user") {
          // user messages: track mid-conversation injections
          const msg = data.message
          if (!msg) continue
          let user_text = ""
          const content = msg.content
          if (typeof content === "string") {
            user_text = content
          } else if (Array.isArray(content)) {
            for (const item of content) {
              if (item && item.type === "text" && item.text) user_text += item.text + " "
            }
          }
          if (user_text.trim() && steps.length > 0) {
            // mid-conversation injection (not the initial prompt)
            steps.push({ files: [], commands: [], thinking: "", text: "", user_injection: user_text.trim().slice(0, 200) })
          }
          continue
        }

        if (data.type !== "assistant") continue
        const msg = data.message
        if (!msg) continue

        // extract model
        const msg_model = msg.model
        if (msg_model && !model) model = msg_model

        // deduplicate token usage by message ID
        const msg_id = msg.id
        if (msg_id && !seen_msg_ids.has(msg_id)) {
          seen_msg_ids.add(msg_id)
          const usage = msg.usage || {}
          const it = usage.input_tokens || 0
          const ot = usage.output_tokens || 0
          const cr = usage.cache_read_input_tokens || 0
          const cc = usage.cache_creation_input_tokens || 0
          total_input_tokens += it
          total_output_tokens += ot
          total_cache_read_tokens += cr
          total_cache_creation_tokens += cc
          if (it > max_input_tokens) max_input_tokens = it
        }

        // step-level data for this message
        const step_files = []
        const step_commands = []
        let step_thinking = ""
        let step_text = ""

        // extract content details
        const content = msg.content
        if (!Array.isArray(content)) continue

        for (const item of content) {
          if (!item || typeof item !== "object") continue

          // thinking block
          if (item.type === "thinking") {
            const thinking_text = item.thinking || ""
            if (thinking_text && thinking_text.length > 15) {
              step_thinking = thinking_text
              if (!first_thinking_found) {
                task_understanding = thinking_text
                first_thinking_found = true
              }
            }
          }
          // text block → decisions and suggestion
          else if (item.type === "text") {
            const text = item.text || ""
            if (text && text.length > 30) decisions.push(text)
            if (text) {
              step_text = text
              last_suggestion = text
            }
          }
          // tool_use block
          else if (item.type === "tool_use") {
            const tool_name = item.name || "unknown"
            const tool_input = item.input || {}

            if (tool_name === "Edit") {
              const fp = tool_input.file_path || ""
              const old_str = tool_input.old_string || ""
              const new_str = tool_input.new_string || ""
              const basename = path.basename(fp) || "unknown"
              const old_lines = old_str ? old_str.split("\n").length : 0
              const new_lines = new_str ? new_str.split("\n").length : 0
              // flat structure (backward compat)
              if (!files_modified[basename]) {
                files_modified[basename] = { lines_added: 0, lines_removed: 0, edit_count: 0 }
              }
              files_modified[basename].lines_added += new_lines
              files_modified[basename].lines_removed += old_lines
              files_modified[basename].edit_count += 1
              // step structure
              step_files.push({ name: basename, action: "edit", added: new_lines, removed: old_lines })
            } else if (tool_name === "Write") {
              const fp = tool_input.file_path || ""
              if (fp) {
                const basename = path.basename(fp)
                files_written.push(basename)
                step_files.push({ name: basename, action: "write" })
              }
            } else if (["Read", "Grep", "Glob"].includes(tool_name)) {
              const fp = tool_input.file_path || ""
              if (fp) {
                const basename = path.basename(fp)
                if (!files_read_set.has(basename)) {
                  files_read_set.add(basename)
                  files_read.push(basename)
                }
                step_files.push({ name: basename, action: "read" })
              }
            } else if (["Bash", "PowerShell"].includes(tool_name)) {
              const desc = tool_input.description || ""
              const cmd = tool_input.command || ""
              const cmd_text = (desc && desc.length > 3) ? desc : cmd
              if (cmd_text) {
                commands_run.push(cmd_text)
                step_commands.push(cmd_text)
              }
            } else if (tool_name === "AskUserQuestion" || tool_name === "TodoWrite") {
              // user-facing question or task tracking: extract as suggestion
              const questions = tool_input.questions || []
              if (questions.length) {
                const q_texts = questions.map((q) => q.question || "").filter(Boolean)
                if (q_texts.length) last_suggestion = q_texts.join("；")
              }
            }
          }
        }

        // add step if it has meaningful content
        if (step_files.length || step_commands.length || (step_thinking && step_thinking.length > 15) || (step_text && step_text.length > 30)) {
          steps.push({ files: step_files, commands: step_commands, thinking: step_thinking, text: step_text })
        }
      }
    } catch (e) {
      this._log("error", `Error parsing transcript ${transcript_path}: ${e.message}`)
      return null
    }

    return {
      task_understanding, decisions, last_suggestion,
      files_modified, files_written, files_read, commands_run,
      steps,
      total_input_tokens, total_output_tokens,
      total_cache_read_tokens, total_cache_creation_tokens,
      max_input_tokens, model,
    }
  }

  // --- AI ---
  async ai_summarize(text, purpose = "general", fallback = null) {
    if (!text) return ""
    const cfg = this.config
    const base_url = cfg.ai_base_url
    const api_key = cfg.ai_api_key
    const model = cfg.ai_model || "claude-sonnet-4-6"

    if (!base_url || !api_key) {
      return !fallback ? this.translate_to_chinese(text) : fallback
    }

    const prompts = {
      task: "根据以下用户输入和模型理解，用一句精简的中文概括这个任务的核心目标和意图。" +
        "优先提取模型理解中的关键信息，结合用户输入补充上下文。" +
        "言简意赅，不超过30字，不要省略号。",
      command: "将以下命令描述用一句精简中文概括做了什么，言简意赅",
      decision: "将以下推理内容用一句精简中文概括核心决策，言简意赅，只说结论",
      ending: "判断以下内容的性质并概括为精简中文。" +
        "如果是后续操作建议，格式：'行动建议：xxx'。" +
        "如果是工作总结，格式：'总结：xxx'。" +
        "如果是分析结论，格式：'结论：xxx'。" +
        "如果是阶段性汇报，格式：'汇报：xxx'。" +
        "言简意赅，不超过30字。",
      change: "将以下代码改动用精简中文逐条概括每项改动的目的，用编号列表格式，每条言简意赅",
      general: "将以下内容用一句精简的中文概括，言简意赅",
    }
    const sys_prompt = prompts[purpose] || prompts.general

    try {
      const url = `${base_url}/v1/messages`
      const payload = {
        model,
        max_tokens: 200,
        messages: [{ role: "user", content: `${sys_prompt}\n\n${text}` }],
      }
      const body_buf = Buffer.from(JSON.stringify(payload), "utf-8")
      const headers = {
        "Content-Type": "application/json; charset=utf-8",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
      }
      const { body: result } = await http_request(url, "POST", headers, body_buf, 5000)
      const content_blocks = result.content || []
      if (content_blocks.length > 0) {
        const summary = (content_blocks[0].text || "").trim()
        if (summary) return summary
      }
    } catch (e) {
      this._log("warning", `AI summarize failed (${purpose}): ${e.message}`)
    }
    return !fallback ? this.translate_to_chinese(text) : fallback
  }

  translate_to_chinese(text) {
    if (!text) return ""
    let result = text.toLowerCase()
    const sorted_keys = Object.keys(EN_ZH_MAP).sort((a, b) => b.length - a.length)
    for (const en of sorted_keys) {
      const zh = EN_ZH_MAP[en]
      if (zh === "") {
        result = result.replace(en + " ", " ")
        result = result.replace(" " + en, " ")
      } else {
        result = result.replace(en, zh)
      }
    }
    return result.replace(/\s+/g, " ").trim()
  }

  summarize_text(text, max_chars = 20) {
    if (!text) return ""
    const first = text.split(".")[0].split(",")[0].trim()
    const translated = this.translate_to_chinese(first)
    if (translated.length <= max_chars) return translated
    return translated.slice(0, max_chars) + "…"
  }

  format_as_bullets(text, max_chars = 30) {
    if (!text) return text
    const segments = text.split(/[。！？；，.!?,;]/).map((s) => s.trim()).filter(Boolean)
    if (segments.length <= 1) return text
    return segments.map((s, i) => `${i + 1}. ${s}`).join("\n")
  }

  strip_markdown_headers(text) {
    if (!text) return text
    return text.split("\n")
      .map((line) => {
        // strip heading markers: ### title → title
        let stripped = line.replace(/^#{1,6}\s+/, "")
        // strip leading bold pseudo-headers: **title** → title
        stripped = stripped.replace(/^\*\*(.+?)\*\*\s*[：:—–-]?\s*/, "$1 ")
        return stripped
      })
      .filter((l) => l.trim())
      .join("\n")
  }

  linkify_urls(text) {
    if (!text) return text
    // replace backtick-wrapped URLs: `https://...` → [display](url)
    text = text.replace(/`(https?:\/\/[^\s`]+)`/g, (match, url) => {
      let display = url.replace(/^https?:\/\//, "").replace(/\/$/, "")
      const parts = display.split("/")
      if (parts.length > 2) display = parts[0] + "/" + parts[parts.length - 1]
      return `[${display}](${url})`
    })
    // replace bare URLs (simple: match https://... not already preceded by ]( )
    // since backtick URLs are already converted above, remaining bare URLs get linkified
    text = text.replace(/(?<![)])((https?:\/\/[^\s<>)]+))/g, (match, p1, url) => `[${url}](${url})`)
    return text
  }

  // --- Card building ---
  build_execution_detail(transcript_data) {
    const consolidated = transcript_data.consolidated_steps || []
    const summaries = transcript_data.step_summaries || []

    if (!consolidated.length) return []

    const sections = []
    for (let i = 0; i < consolidated.length; i++) {
      const step = consolidated[i]

      // user injection step — show separately
      if (step.user_injection) {
        sections.push(`<font color='blue'>**用户补充**</font>：${step.user_injection.slice(0, 80)}`)
        continue
      }

      const step_desc = i < summaries.length ? summaries[i] : this._translate_step_desc(step)

      // file operations (chronological within step, colored by action type)
      const file_lines = []
      for (const f of (step.files || [])) {
        const action = f.action || "read"
        const name = f.name || ""
        if (action === "edit") {
          const added = f.added || 0
          const removed = f.removed || 0
          let change_str
          if (removed > 0) {
            change_str = `**<font color='green'>+${added}</font><font color='red'>/-${removed}</font>**`
          } else {
            change_str = `**<font color='green'>+${added}</font>**`
          }
          file_lines.push(`<font color='yellow'>编辑</font> \`${name}\` ${change_str}`)
        } else if (action === "write") {
          file_lines.push(`<font color='green'>新建</font> \`${name}\``)
        } else if (action === "read") {
          file_lines.push(`<font color='grey'>读取</font> \`${name}\``)
        }
      }

      // commands (translated to Chinese)
      const cmd_lines = []
      for (const cmd of (step.commands || [])) {
        cmd_lines.push(`\`${this.translate_to_chinese(cmd)}\``)
      }

      // combine into section
      const parts = []
      if (step_desc) {
        parts.push(`**第${i + 1}步：${step_desc}**`)
      } else {
        const actions = (step.files || []).map((f) => f.action)
        if (actions.includes("edit")) parts.push(`**第${i + 1}步：修改代码**`)
        else if (actions.includes("write")) parts.push(`**第${i + 1}步：创建文件**`)
        else if (actions.includes("read")) parts.push(`**第${i + 1}步：调研分析**`)
        else parts.push(`**第${i + 1}步**`)
      }

      if (file_lines.length) parts.push(file_lines.join("\n"))
      if (cmd_lines.length) parts.push(cmd_lines.join("\n"))

      const section_content = parts.join("\n")
      if (section_content.trim()) sections.push(section_content)
    }

    return sections
  }

  _consolidate_steps(steps, limit = 6) {
    const consolidated = []
    const pending_reads = []

    for (const step of steps) {
      const has_edits = (step.files || []).some((f) => f.action === "edit")
      const has_writes = (step.files || []).some((f) => f.action === "write")
      const has_commands = (step.commands || []).length > 0

      if (has_edits || has_writes || has_commands) {
        // attach pending reads to this significant step
        if (pending_reads.length) {
          const read_names = []
          for (const rs of pending_reads) {
            for (const f of (rs.files || [])) {
              if (f.action === "read" && !read_names.includes(f.name)) read_names.push(f.name)
            }
          }
          if (read_names.length) {
            const existing_names = (step.files || []).map((f) => f.name)
            const read_entries = read_names.filter((n) => !existing_names.includes(n)).map((n) => ({ name: n, action: "read" }))
            step.files = read_entries.concat(step.files || [])
          }
          // use thinking from pending reads if step has none
          if (!step.thinking) {
            for (const rs of pending_reads) {
              if (rs.thinking) { step.thinking = rs.thinking; break }
            }
          }
          pending_reads.length = 0
        }
        consolidated.push(step)
      } else {
        pending_reads.push(step)
      }
    }

    // remaining reads without any significant step
    if (pending_reads.length) {
      const read_names = []
      for (const rs of pending_reads) {
        for (const f of (rs.files || [])) {
          if (f.action === "read" && !read_names.includes(f.name)) read_names.push(f.name)
        }
      }
      if (read_names.length) {
        consolidated.push({ files: read_names.map((n) => ({ name: n, action: "read" })), commands: [], thinking: "", text: "" })
      }
    }

    return consolidated.slice(0, limit)
  }

  async ai_summarize_steps(steps) {
    const cfg = this.config
    const base_url = cfg.ai_base_url
    const api_key = cfg.ai_api_key
    const model = cfg.ai_model || "claude-sonnet-4-6"

    if (!base_url || !api_key) {
      return steps.map((step) => this._translate_step_desc(step))
    }

    // build combined input text
    let raw_text = ""
    for (let i = 0; i < steps.length; i++) {
      const step = steps[i]
      const parts = []
      if (step.user_injection) {
        parts.push(`用户补充消息: ${step.user_injection.slice(0, 100)}`)
      }
      if (step.thinking) {
        const clean = this.strip_markdown_headers(step.thinking)
        const first = clean.split("\n")[0].trim().slice(0, 200)
        parts.push(first)
      }
      if (step.text && !parts.length) {
        const clean = this.strip_markdown_headers(step.text)
        const first = clean.split("\n")[0].trim().slice(0, 200)
        parts.push(first)
      }
      for (const f of (step.files || [])) {
        const action_zh = { edit: "编辑", write: "新建", read: "读取" }[f.action] || "操作"
        parts.push(`${action_zh} ${f.name || ""}`)
      }
      for (const cmd of (step.commands || [])) {
        parts.push(cmd.slice(0, 100))
      }
      raw_text += `步骤${i + 1}: ${parts.join(" | ")}\n`
    }

    const sys_prompt = "将以下执行步骤逐条概括为精简中文，每条一行，格式为'第X步：中文概括'。" +
      "面向非技术人员，用通俗语言解释每步做了什么、为什么做。" +
      "言简意赅，每条不超过30字。只输出概括列表，不要其他内容。"

    try {
      const url = `${base_url}/v1/messages`
      const payload = {
        model,
        max_tokens: 300,
        messages: [{ role: "user", content: `${sys_prompt}\n\n${raw_text}` }],
      }
      const body_buf = Buffer.from(JSON.stringify(payload), "utf-8")
      const headers = {
        "Content-Type": "application/json; charset=utf-8",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
      }
      const { body: result } = await http_request(url, "POST", headers, body_buf, 5000)
      const content_blocks = result.content || []
      if (content_blocks.length > 0) {
        const summary_text = (content_blocks[0].text || "").trim()
        const descriptions = []
        for (const line of summary_text.split("\n")) {
          const trimmed = line.trim()
          if (trimmed) {
            let desc = trimmed.replace(/^第\d+步[：:]\s*/, "")
            desc = desc.replace(/^\d+[\.。、]\s*/, "")
            if (desc) descriptions.push(desc)
          }
        }
        if (descriptions.length >= steps.length) {
          return descriptions.slice(0, steps.length)
        }
        // pad missing descriptions with fallback
        while (descriptions.length < steps.length) {
          descriptions.push(this._translate_step_desc(steps[descriptions.length]))
        }
        return descriptions
      }
    } catch (e) {
      this._log("warning", `AI step summarization failed: ${e.message}`)
    }

    return steps.map((step) => this._translate_step_desc(step))
  }

  _translate_step_desc(step) {
    if (step.user_injection) return `用户补充：${step.user_injection.slice(0, 30)}`

    const files = step.files || []
    const commands = step.commands || []
    const actions = files.map((f) => f.action)

    // edit step: show file names for specificity
    if (actions.includes("edit")) {
      const edit_names = files.filter((f) => f.action === "edit").map((f) => f.name)
      if (edit_names.length <= 3) return `修改 ${edit_names.join("、")}`
      return `修改 ${edit_names.length} 个文件`
    }

    // write step: show file names
    if (actions.includes("write")) {
      const write_names = files.filter((f) => f.action === "write").map((f) => f.name)
      if (write_names.length <= 3) return `创建 ${write_names.join("、")}`
      return `创建 ${write_names.length} 个文件`
    }

    // command step
    if (commands.length) return "执行操作"

    // read step
    if (actions.includes("read")) return "调研分析"

    // text/thinking only: show only if short enough to be readable, else generic label
    if (step.thinking) {
      const clean = this.strip_markdown_headers(step.thinking)
      const first = clean.split("\n")[0].trim()
      if (first.length <= 30) return first
      return "分析思考"
    }
    if (step.text) {
      const clean = this.strip_markdown_headers(step.text)
      const first = clean.split("\n")[0].trim()
      if (first.length <= 30) return first
      return "输出结果"
    }

    return "处理任务"
  }

  classify_ending_label(text) {
    if (!text) return "结果汇总"
    const lower = text.toLowerCase()
    if (/conclusion|therefore|thus|结论/.test(lower)) return "结论"
    if (/summary|overview|总结|回顾/.test(lower)) return "总结"
    if (/report|progress|汇报|进展/.test(lower)) return "汇报"
    if (/suggest|recommend|should|next|请|下一步/.test(lower)) return "行动建议"
    // completed task: default to 结果汇总
    return "结果汇总"
  }

  generate_task_summary_fallback(prompt) {
    if (!prompt) return ""
    const trimmed = prompt.trim()
    // show full prompt if short enough, otherwise truncate whole text
    if (trimmed.length <= 60) return trimmed
    return trimmed.slice(0, 57) + "..."
  }

  normalize_model_name(model) {
    if (!model) return model
    // strip -latest suffix
    let stripped = model.replace(/-latest$/, "")
    // strip @version suffix (used by some providers)
    stripped = stripped.replace(/@[\w-]+$/, "")
    // strip trailing date suffix: -YYYYMMDD or -YYYY-MM-DD
    stripped = stripped.replace(/-(\d{4})(\d{2})(\d{2})$/, "")
    stripped = stripped.replace(/-(\d{4})-(\d{2})-(\d{2})$/, "")
    return stripped
  }

  estimate_cost(total_input, total_output, model, cache_read = 0, cache_creation = 0) {
    if (!model) return "unknown"
    const pricing = {
      // --- Anthropic Claude ---
      "claude-opus-4-7": { input: 15, output: 75, cache_read: 1.875, cache_creation: 18.75 },
      "claude-sonnet-4-6": { input: 3, output: 15, cache_read: 0.30, cache_creation: 3.75 },
      "claude-haiku-4-5": { input: 0.80, output: 4, cache_read: 0.08, cache_creation: 0.80 },

      // --- OpenAI GPT ---
      "gpt-4.5-preview": { input: 75, output: 150, cache_read: 37.50, cache_creation: 75 },
      "gpt-4o": { input: 5, output: 15, cache_read: 1.25, cache_creation: 2.50 },
      "gpt-4o-mini": { input: 0.15, output: 0.60, cache_read: 0.075, cache_creation: 0.15 },
      "o1": { input: 15, output: 60, cache_read: 7.50, cache_creation: 15 },
      "o1-mini": { input: 1.10, output: 4.40, cache_read: 0.55, cache_creation: 1.10 },
      "o3-mini": { input: 1.10, output: 4.40, cache_read: 0.55, cache_creation: 1.10 },

      // --- DeepSeek ---
      "deepseek-chat": { input: 0.14, output: 0.28, cache_read: 0.014, cache_creation: 0.14 },
      "deepseek-reasoner": { input: 0.55, output: 2.19, cache_read: 0.14, cache_creation: 0.55 },

      // --- Moonshot Kimi ---
      "kimi-moonshot-v1-8k": { input: 1.67, output: 1.67, cache_read: 0.17, cache_creation: 1.67 },
      "kimi-moonshot-v1-32k": { input: 1.67, output: 1.67, cache_read: 0.17, cache_creation: 1.67 },
      "kimi-moonshot-v1-128k": { input: 1.67, output: 1.67, cache_read: 0.17, cache_creation: 1.67 },
      "kimi-latest": { input: 1.67, output: 1.67, cache_read: 0.17, cache_creation: 1.67 },
      "kimi-for-coding": { input: 2, output: 2, cache_read: 0.20, cache_creation: 2.00 },
      "kimi-k1": { input: 1.67, output: 1.67, cache_read: 0.17, cache_creation: 1.67 },
      "kimi-k1.5": { input: 1.67, output: 1.67, cache_read: 0.17, cache_creation: 1.67 },

      // --- Alibaba Qwen ---
      "qwen-max": { input: 2.40, output: 4.80, cache_read: 1.20, cache_creation: 2.40 },
      "qwen-plus": { input: 0.80, output: 2.00, cache_read: 0.40, cache_creation: 0.80 },
      "qwen-turbo": { input: 0.30, output: 0.60, cache_read: 0.15, cache_creation: 0.30 },
      "qwen-coder-plus": { input: 0.70, output: 1.40, cache_read: 0.35, cache_creation: 0.70 },
      "qwen-coder-turbo": { input: 0.20, output: 0.60, cache_read: 0.10, cache_creation: 0.20 },

      // --- Zhipu GLM ---
      "glm-5.1": { input: 15, output: 75, cache_read: 1.875, cache_creation: 18.75 },
      "glm-4": { input: 1.00, output: 1.00, cache_read: 0.10, cache_creation: 1.00 },
      "glm-4-plus": { input: 2.00, output: 2.00, cache_read: 0.20, cache_creation: 2.00 },
      "glm-4-flash": { input: 0.05, output: 0.05, cache_read: 0.005, cache_creation: 0.05 },
      "glm-4-air": { input: 0.10, output: 0.10, cache_read: 0.01, cache_creation: 0.10 },

      // --- MiniMax ---
      "abab6.5": { input: 2.40, output: 2.40, cache_read: 0.24, cache_creation: 2.40 },
      "abab6.5s": { input: 0.80, output: 0.80, cache_read: 0.08, cache_creation: 0.80 },
      "minimax-text-01": { input: 0.13, output: 0.13, cache_read: 0.013, cache_creation: 0.13 },

      // --- Xiaomi ---
      "xiaomi-mi": { input: 2.00, output: 2.00, cache_read: 0.20, cache_creation: 2.00 },
    }
    const normalized = this.normalize_model_name(model)
    let mp = pricing[normalized]
    if (!mp) {
      // sort keys by length descending for more specific prefix matches first
      const keys = Object.keys(pricing).sort((a, b) => b.length - a.length)
      for (const key of keys) {
        if (normalized.startsWith(key) || key.startsWith(normalized)) {
          mp = pricing[key]
          break
        }
      }
    }
    if (!mp) {
      // unknown model: return token counts as fallback
      const total = total_input + total_output + cache_read + cache_creation
      if (total > 0) {
        const parts = []
        if (total_input > 0) parts.push(`in:${this._format_token_count(total_input)}`)
        if (total_output > 0) parts.push(`out:${this._format_token_count(total_output)}`)
        if (cache_read > 0) parts.push(`cr:${this._format_token_count(cache_read)}`)
        if (cache_creation > 0) parts.push(`cc:${this._format_token_count(cache_creation)}`)
        return parts.length ? parts.join(" / ") : "unknown"
      }
      return "unknown"
    }
    const cost = total_input / 1e6 * mp.input
      + total_output / 1e6 * mp.output
      + cache_read / 1e6 * mp.cache_read
      + cache_creation / 1e6 * mp.cache_creation
    if (cost < 0.01) return `$${cost.toFixed(4)}`
    if (cost < 1) return `$${cost.toFixed(2)}`
    return `$${cost.toFixed(1)}`
  }

  _format_token_count(tokens) {
    if (tokens >= 1e6) return `${(tokens / 1e6).toFixed(1)}M`
    if (tokens >= 1e3) return `${Math.round(tokens / 1e3)}K`
    return String(tokens)
  }

  format_cost_value(cost_val) {
    if (cost_val == null) return "unknown"
    if (typeof cost_val === "string") return cost_val
    if (cost_val < 0.01) return `$${cost_val.toFixed(4)}`
    if (cost_val < 1) return `$${cost_val.toFixed(2)}`
    return `$${cost_val.toFixed(1)}`
  }

  format_context_pct(max_input_tokens, model) {
    if (!max_input_tokens) return null
    const context_windows = {
      // --- Anthropic Claude ---
      "claude-opus-4-7": 1e6,
      "claude-sonnet-4-6": 2e5,
      "claude-haiku-4-5": 2e5,

      // --- OpenAI GPT ---
      "gpt-4.5-preview": 128e3,
      "gpt-4o": 128e3,
      "gpt-4o-mini": 128e3,
      "o1": 2e5,
      "o1-mini": 128e3,
      "o3-mini": 2e5,

      // --- DeepSeek ---
      "deepseek-chat": 64e3,
      "deepseek-reasoner": 64e3,

      // --- Moonshot Kimi ---
      "kimi-moonshot-v1-8k": 8192,
      "kimi-moonshot-v1-32k": 32768,
      "kimi-moonshot-v1-128k": 131072,
      "kimi-latest": 256e3,
      "kimi-for-coding": 256e3,
      "kimi-k1": 131072,
      "kimi-k1.5": 256e3,

      // --- Alibaba Qwen ---
      "qwen-max": 32768,
      "qwen-plus": 131072,
      "qwen-turbo": 131072,
      "qwen-coder-plus": 131072,
      "qwen-coder-turbo": 131072,

      // --- Zhipu GLM ---
      "glm-5.1": 1e6,
      "glm-4": 131072,
      "glm-4-plus": 131072,
      "glm-4-flash": 131072,
      "glm-4-air": 131072,

      // --- MiniMax ---
      "abab6.5": 8192,
      "abab6.5s": 8192,
      "minimax-text-01": 1e6,

      // --- Xiaomi ---
      "xiaomi-mi": 128e3,
    }
    const normalized = model ? this.normalize_model_name(model) : null
    let ctx_size = normalized ? context_windows[normalized] : null
    if (!ctx_size && normalized) {
      // sort keys by length descending for more specific prefix matches first
      const keys = Object.keys(context_windows).sort((a, b) => b.length - a.length)
      for (const key of keys) {
        if (normalized.startsWith(key) || key.startsWith(normalized)) {
          ctx_size = context_windows[key]
          break
        }
      }
    }
    if (!ctx_size) {
      // unknown model: show raw token count instead of "unknown"
      return `${this._format_token_count(max_input_tokens)} used`
    }
    const pct = (max_input_tokens / ctx_size) * 100
    return `${this._format_token_count(max_input_tokens)}/${this._format_token_count(ctx_size)} (${pct.toFixed(1)}%)`
  }

  build_feishu_card(opts) {
    const {
      workspace, notification_type, content_lines,
      task_understanding, task_ai_processed, execution_detail,
      cost, cumulative_cost, context_pct,
      suggestion, ending_label, prompt_summary, model,
      duration, seq,
    } = opts

    const color_map = { success: "green", warning: "yellow", urgent: "red", info: "blue" }
    const template = color_map[notification_type] || "blue"
    const bg_map = { success: "green-300", warning: "orange-300", urgent: "red-300", info: "blue-300" }
    const bg_style = bg_map[notification_type] || "blue-300"
    const label_map = { success: "已完成", warning: "需操作", urgent: "需授权", info: "通知" }
    const status_label = label_map[notification_type] || "通知"

    // main content
    let main_md = content_lines.join("\n")
    if (task_understanding) {
      const label = task_ai_processed ? "任务AI理解" : "任务理解"
      main_md += `\n<br><br>\n>**${label}**\n- ${task_understanding}`
    } else if (prompt_summary) {
      main_md += `\n<br><br>\n>**任务摘要**\n- \`${prompt_summary}\``
    }

    const body_elements = [
      { tag: "markdown", content: main_md, text_align: "left", text_size: "normal_v2", margin: "0px 0px 0px 0px" },
    ]

    // execution detail sections
    if (execution_detail) {
      for (const section of execution_detail) {
        body_elements.push({ tag: "hr", margin: "0px 0px 0px 0px" })
        body_elements.push({ tag: "markdown", content: ">" + section, text_align: "left", text_size: "normal_v2", margin: "0px 0px 0px 0px" })
      }
    }

    // ending section (dynamic label)
    if (suggestion) {
      body_elements.push({ tag: "hr", margin: "0px 0px 0px 0px" })
      const label = ending_label || "建议"
      body_elements.push({ tag: "markdown", content: `>**${label}**\n${suggestion}`, text_align: "left", text_size: "normal_v2", margin: "0px 0px 0px 0px" })
    }

    // badge row
    const status_text = seq ? `${status_label} #${seq}` : status_label
    const badge_columns = [
      {
        tag: "column", width: "auto", background_style: bg_style,
        elements: [{ tag: "markdown", content: `<font color='white'>${status_text}</font>`, text_align: "center", text_size: "notation", margin: "0px 0px 0px 0px" }],
        padding: "0px 8px 0px 8px", direction: "horizontal", horizontal_spacing: "8px", vertical_spacing: "0px", horizontal_align: "left", vertical_align: "top", margin: "0px 0px 0px 0px",
      },
    ]

    // duration badge
    if (duration) {
      badge_columns.push({
        tag: "column", width: "auto",
        elements: [{ tag: "markdown", content: `<font color='grey'>${duration}</font>`, text_align: "center", text_size: "notation", margin: "0px 0px 0px 0px" }],
        padding: "0px 8px 0px 8px", horizontal_spacing: "8px", vertical_spacing: "0px", horizontal_align: "left", vertical_align: "top", margin: "0px 0px 0px 0px",
      })
    }

    // model badge (normalized, no prefix)
    const display_model = model ? this.normalize_model_name(model) : null
    badge_columns.push({
      tag: "column", width: "auto",
      elements: [{ tag: "markdown", content: display_model ? `<font color='grey'>${display_model}</font>` : "<font color='grey'>---</font>", text_align: "center", text_size: "notation", margin: "0px 0px 0px 0px" }],
      padding: "0px 8px 0px 8px", horizontal_spacing: "8px", vertical_spacing: "0px", horizontal_align: "left", vertical_align: "top", margin: "0px 0px 0px 0px",
    })

    // cost badge (per-round/cumulative)
    let cost_color = "grey"
    let cost_display = "---"
    if (cost && cost !== "unknown") {
      try {
        const cv = parseFloat(cost.replace("$", ""))
        if (cv < 1) cost_color = "green"
        else if (cv < 5) cost_color = "orange"
        else cost_color = "red"
      } catch { cost_color = "grey" }
      cost_display = cumulative_cost && cumulative_cost !== "unknown" ? `${cost}/${cumulative_cost}` : cost
    } else if (cost === "unknown") {
      cost_display = "unknown"
    }
    badge_columns.push({
      tag: "column", width: "auto",
      elements: [{ tag: "markdown", content: `<font color='${cost_color}'>${cost_display}</font>`, text_align: "center", text_size: "notation", margin: "0px 0px 0px 0px" }],
      padding: "0px 8px 0px 8px", horizontal_spacing: "8px", vertical_spacing: "0px", horizontal_align: "left", vertical_align: "top", margin: "0px 0px 0px 0px",
    })

    // context badge
    let ctx_color = "grey"
    let ctx_display = "---"
    if (context_pct) {
      if (context_pct !== "unknown") {
        const pct_match = context_pct.match(/([\d.]+)%/)
        const pct_val = pct_match ? parseFloat(pct_match[1]) : 0
        ctx_color = pct_val < 30 ? "green" : pct_val < 70 ? "orange" : "red"
      }
      ctx_display = context_pct
    }
    badge_columns.push({
      tag: "column", width: "auto",
      elements: [{ tag: "markdown", content: `<font color='${ctx_color}'>${ctx_display}</font>`, text_align: "center", text_size: "notation", margin: "0px 0px 0px 0px" }],
      padding: "0px 8px 0px 8px", horizontal_spacing: "8px", vertical_spacing: "0px", horizontal_align: "left", vertical_align: "top", margin: "0px 0px 0px 0px",
    })

    // timestamp badge
    const current_time = now_iso()
    badge_columns.push({
      tag: "column", width: "auto",
      elements: [{ tag: "markdown", content: `<font color='grey'>${current_time}</font>`, text_align: "center", text_size: "notation", margin: "0px 0px 0px 0px" }],
      padding: "0px 8px 0px 8px", direction: "vertical", horizontal_spacing: "8px", vertical_spacing: "8px", horizontal_align: "left", vertical_align: "top", margin: "0px 0px 0px 0px",
    })

    body_elements.push({ tag: "hr", margin: "0px 0px 0px 0px" })
    body_elements.push({ tag: "column_set", horizontal_spacing: "8px", horizontal_align: "left", columns: badge_columns, margin: "0px 0px 0px 0px" })

    const card = {
      schema: "2.0",
      config: { update_multi: true, style: { text_size: { normal_v2: { default: "normal", pc: "normal", mobile: "heading" } } } },
      header: { title: { tag: "plain_text", content: workspace }, template },
      body: { direction: "vertical", padding: "12px 12px 12px 12px", elements: body_elements },
    }

    this._log("info", `[CARD] task_understanding=${(task_understanding || "None").slice(0, 80)}, cost=${cost}/${cumulative_cost}, model=${model}, ctx=${context_pct}`)
    return card
  }

  // --- Feishu API ---
  async get_tenant_access_token() {
    if (this._tenant_token && Date.now() < this._token_expire_at) return this._tenant_token

    const cfg = this.config
    const url = `${FEISHU_APP_API_BASE}/auth/v3/tenant_access_token/internal`
    const payload = { app_id: cfg.app_id, app_secret: cfg.app_secret }
    const body_buf = Buffer.from(JSON.stringify(payload), "utf-8")
    const headers = { "Content-Type": "application/json" }

    try {
      const { body: result } = await http_request(url, "POST", headers, body_buf, 10000)
      if (result.code !== 0) {
        this._log("error", `Failed to get tenant_access_token: code=${result.code}, msg=${result.msg}`)
        return null
      }
      this._tenant_token = result.tenant_access_token
      const expire = result.expire || 7200
      this._token_expire_at = Date.now() + (expire - 300) * 1000
      this._log("info", "Tenant access token refreshed successfully")
      return this._tenant_token
    } catch (e) {
      this._log("error", `Error getting tenant_access_token: ${e.message}`)
      return null
    }
  }

  async send_via_app_mode(card) {
    const token = await this.get_tenant_access_token()
    if (!token) {
      this._log("error", "Cannot send notification: no tenant_access_token")
      return [false, null]
    }

    const cfg = this.config
    const receive_id_type = cfg.receive_id_type || "open_id"
    const url = `${FEISHU_APP_API_BASE}/im/v1/messages?receive_id_type=${receive_id_type}`
    const content_str = JSON.stringify(card)
    const payload = { receive_id: cfg.receive_id, msg_type: "interactive", content: content_str }
    const body_buf = Buffer.from(JSON.stringify(payload), "utf-8")
    const headers = {
      "Content-Type": "application/json; charset=utf-8",
      "Authorization": `Bearer ${token}`,
    }

    try {
      const { body: result } = await http_request(url, "POST", headers, body_buf, 10000)
      if (result.code !== 0) {
        this._log("error", `Feishu IM API error: code=${result.code}, msg=${result.msg}`)
        return [false, null]
      }
      const message_id = (result.data || {}).message_id
      this._log("info", `Feishu card delivered to ${cfg.receive_id} (App mode), msg_id=${message_id}`)
      return [true, message_id]
    } catch (e) {
      this._log("error", `Feishu IM API request failed: ${e.message}`)
      this._tenant_token = null
      this._token_expire_at = 0
      return [false, null]
    }
  }

  async update_card(message_id, card) {
    const token = await this.get_tenant_access_token()
    if (!token || !message_id) return false

    const url = `${FEISHU_APP_API_BASE}/im/v1/messages/${message_id}`
    const content_str = JSON.stringify(card)
    const payload = { msg_type: "interactive", content: content_str }
    const body_buf = Buffer.from(JSON.stringify(payload), "utf-8")
    const headers = {
      "Content-Type": "application/json; charset=utf-8",
      "Authorization": `Bearer ${token}`,
    }

    try {
      const { body: result } = await http_request(url, "PATCH", headers, body_buf, 10000)
      if (result.code !== 0) {
        this._log("error", `Feishu card update failed: code=${result.code}, msg=${result.msg}`)
        return false
      }
      this._log("info", `Feishu card updated, msg_id=${message_id}`)
      return true
    } catch (e) {
      this._log("error", `Error updating Feishu card: ${e.message}`)
      return false
    }
  }

  async send_via_webhook_mode(card) {
    const cfg = this.config
    const webhook_url = cfg.webhook_url
    const payload = { msg_type: "interactive", card }

    const webhook_secret = cfg.webhook_secret || ""
    if (webhook_secret) {
      const { timestamp, sign } = this.generate_sign(webhook_secret)
      payload.timestamp = timestamp
      payload.sign = sign
    }

    const body_buf = Buffer.from(JSON.stringify(payload), "utf-8")
    const headers = { "Content-Type": "application/json" }

    try {
      const { body: result } = await http_request(webhook_url, "POST", headers, body_buf, 10000)
      const code = result.code !== undefined ? result.code : (result.StatusCode !== undefined ? result.StatusCode : -1)
      if (code !== 0) {
        this._log("error", `Feishu webhook API error: code=${code}, msg=${result.msg || "unknown"}`)
        return false
      }
      this._log("info", "Feishu card delivered (webhook mode)")
      return true
    } catch (e) {
      this._log("error", `Feishu webhook request failed: ${e.message}`)
      return false
    }
  }

  save_summary_queue(session_id, workspace, seq, message_id, duration, cost, cumulative_cost, context_pct, transcript_data) {
    const queue_path = path.join(this.project_dir, "db", "summary_queue.json")
    let queue = []
    if (fs.existsSync(queue_path)) {
      try { queue = JSON.parse(fs.readFileSync(queue_path, "utf-8")) } catch { queue = [] }
    }
    queue.push({
      session_id, workspace, seq, message_id, duration, cost, cumulative_cost, context_pct,
      timestamp: now_iso(),
      raw_data: {
        task_understanding: transcript_data.task_understanding || "",
        commands_run: transcript_data.commands_run || [],
        decisions: transcript_data.decisions || [],
        last_suggestion: transcript_data.last_suggestion || "",
        files_modified: transcript_data.files_modified || {},
        files_written: transcript_data.files_written || [],
      },
      status: "pending",
    })
    // keep only last 10 pending items
    queue = queue.filter((item) => item.status !== "done").slice(-10)
    fs.writeFileSync(queue_path, JSON.stringify(queue, null, 2), "utf-8")
  }

  async send_notification(opts) {
    if (!this.send_mode) {
      this._log("warning", "Feishu notification not configured. Set webhook_url or (app_id + app_secret + receive_id) in config/env.")
      return [false, null]
    }

    const card = this.build_feishu_card(opts)

    if (this.send_mode === "app") return this.send_via_app_mode(card)
    if (this.send_mode === "webhook") {
      const result = await this.send_via_webhook_mode(card)
      return [result, null]
    }
    return [false, null]
  }
}

// --- Utility ---
function sanitize_surrogates(obj) {
  if (typeof obj === "string") {
    return Buffer.from(obj, "utf-8").toString("utf-8")
  } else if (Array.isArray(obj)) {
    return obj.map(sanitize_surrogates)
  } else if (obj && typeof obj === "object") {
    const result = {}
    for (const key in obj) result[key] = sanitize_surrogates(obj[key])
    return result
  }
  return obj
}

function validate_input_data(data, expected_event_name) {
  const required_fields = {
    UserPromptSubmit: ["session_id", "prompt", "cwd", "hook_event_name"],
    Stop: ["session_id", "hook_event_name"],
    Notification: ["session_id", "message", "hook_event_name"],
  }
  if (!required_fields[expected_event_name]) throw new Error(`Unknown event type: ${expected_event_name}`)
  if (data.hook_event_name !== expected_event_name) throw new Error(`Event name mismatch: expected ${expected_event_name}, got ${data.hook_event_name}`)
  const missing = required_fields[expected_event_name].filter((f) => data[f] == null)
  if (missing.length) throw new Error(`Missing required fields for ${expected_event_name}: ${missing.join(",")}`)
}

// --- Hook self-registration ---
// cc-switch may overwrite ~/.claude/settings.json and remove hooks.
// This function ensures ccfeishunotify hooks are present.
function ensure_hooks_in_file(file_path) {
  if (!fs.existsSync(file_path)) return false

  try {
    const content = JSON.parse(fs.readFileSync(file_path, "utf-8"))
    if (!content.hooks) content.hooks = {}

    const tracker = new ClaudePromptTracker()
    const script_path = path.join(tracker.project_dir, "src", "ccfeishunotify.js")
    const hook_cmd = `node "${script_path}"`

    const events = ["UserPromptSubmit", "Stop", "Notification"]
    let needs_update = false

    for (const event of events) {
      if (!content.hooks[event]) {
        content.hooks[event] = [{
          matcher: "",
          hooks: [{ type: "command", command: `${hook_cmd} ${event}` }],
        }]
        needs_update = true
      } else {
        const hook_groups = content.hooks[event]
        const found = hook_groups.some((group) =>
          group.hooks && group.hooks.some((h) => h.command && h.command.includes("ccfeishunotify"))
        )
        if (!found) {
          hook_groups.push({
            matcher: "",
            hooks: [{ type: "command", command: `${hook_cmd} ${event}` }],
          })
          needs_update = true
        }
      }
    }

    if (needs_update) {
      fs.writeFileSync(file_path, JSON.stringify(content, null, 2), "utf-8")
      return true
    }
    return false
  } catch (e) {
    // don't use this._log here (static function, no tracker instance)
    console.error(`Warning: could not check/register hooks in ${file_path}: ${e.message}`)
    return false
  }
}

function ensure_hooks_registered() {
  const home = os.homedir()
  const claude_dir = path.join(home, ".claude")
  // check both settings.json and settings.local.json
  // cc-switch may overwrite one of them, so we ensure hooks in both
  const files = [
    path.join(claude_dir, "settings.json"),
    path.join(claude_dir, "settings.local.json"),
  ]
  for (const f of files) {
    const updated = ensure_hooks_in_file(f)
    if (updated) console.log(`hooks registered in ${path.basename(f)}`)
  }
}

// --- CC-Switch database hook injection ---
// Inject hooks into every Claude provider in cc-switch's SQLite database,
// so switching providers never loses our hooks.
function ensure_hooks_in_ccswitch() {
  const home = os.homedir()
  const db_path = path.join(home, ".cc-switch", "cc-switch.db")
  if (!fs.existsSync(db_path)) return // cc-switch not installed

  // node:sqlite requires Node.js v22.5+; skip gracefully on older versions
  let sqlite
  try { sqlite = require("node:sqlite") } catch { return }

  try {
    const db = new sqlite.DatabaseSync(db_path)

    // read all Claude providers
    const providers = db.prepare("SELECT id, name, settings_config FROM providers WHERE app_type='claude'").all()

    // build our hooks template
    const tracker = new ClaudePromptTracker()
    const script_path = path.join(tracker.project_dir, "src", "ccfeishunotify.js")
    const hook_cmd = `node "${script_path}"`

    const hooks_template = {
      UserPromptSubmit: [{ matcher: "", hooks: [{ type: "command", command: `${hook_cmd} UserPromptSubmit` }] }],
      Stop: [{ matcher: "", hooks: [{ type: "command", command: `${hook_cmd} Stop` }] }],
      Notification: [{ matcher: "", hooks: [{ type: "command", command: `${hook_cmd} Notification` }] }],
    }

    let updated_count = 0
    const update_stmt = db.prepare("UPDATE providers SET settings_config = ? WHERE id = ?")

    for (const provider of providers) {
      let config
      try {
        const raw = provider.settings_config
        config = raw ? JSON.parse(raw) : {}
      } catch { config = {} }

      // check if our hooks are already present
      let needs_update = false
      if (!config.hooks) {
        config.hooks = hooks_template
        needs_update = true
      } else {
        for (const event of ["UserPromptSubmit", "Stop", "Notification"]) {
          if (!config.hooks[event]) {
            config.hooks[event] = hooks_template[event]
            needs_update = true
          } else {
            const hook_groups = config.hooks[event]
            const found = hook_groups.some((group) =>
              group.hooks && group.hooks.some((h) => h.command && h.command.includes("ccfeishunotify"))
            )
            if (!found) {
              hook_groups.push(hooks_template[event][0])
              needs_update = true
            }
          }
        }
      }

      if (needs_update) {
        update_stmt.run(JSON.stringify(config), provider.id)
        updated_count++
      }
    }

    db.close()
    if (updated_count > 0) console.log(`hooks injected into ${updated_count} cc-switch providers`)
  } catch (e) {
    // cc-switch database access failed — non-critical, our settings.json hooks still work
    console.error(`Warning: cc-switch db injection failed: ${e.message}`)
  }
}

// --- Main ---
async function main() {
  try {
    if (process.argv.length < 3) {
      // standalone mode: check health + ensure hooks registered (cc-switch compatibility)
      ensure_hooks_registered()
      ensure_hooks_in_ccswitch()
      console.log("ok")
      return
    }

    const expected_event_name = process.argv[2]

    // read stdin
    const chunks = []
    process.stdin.setEncoding("utf-8")
    process.stdin.on("data", (chunk) => chunks.push(chunk))
    await new Promise((resolve) => process.stdin.on("end", resolve))
    const input_str = chunks.join("").trim()

    // UpdateCard: external command to update a card with AI summaries
    if (expected_event_name === "UpdateCard") {
      if (!input_str) {
        console.error("No input data for UpdateCard")
        process.exit(1)
      }
      const data = sanitize_surrogates(JSON.parse(input_str))
      const tracker = new ClaudePromptTracker()
      const message_id = data.message_id
      if (!message_id) {
        console.error("UpdateCard requires message_id")
        process.exit(1)
      }

      const card = tracker.build_feishu_card({
        workspace: data.workspace || "unknown",
        notification_type: data.notification_type || "success",
        content_lines: data.content_lines || [],
        task_understanding: data.task_understanding,
        execution_detail: data.execution_detail,
        cost: data.cost,
        cumulative_cost: data.cumulative_cost,
        context_pct: data.context_pct,
        suggestion: data.suggestion,
        model: data.model,
        duration: data.duration,
        seq: data.seq,
      })

      const success = await tracker.update_card(message_id, card)
      if (success) {
        // mark queue item as done
        const queue_path = path.join(tracker.project_dir, "db", "summary_queue.json")
        if (fs.existsSync(queue_path)) {
          try {
            const queue = JSON.parse(fs.readFileSync(queue_path, "utf-8"))
            for (const item of queue) {
              if (item.message_id === message_id) item.status = "done"
            }
            fs.writeFileSync(queue_path, JSON.stringify(queue, null, 2), "utf-8")
          } catch (e) {
            tracker._log("warning", `Failed to update queue: ${e.message}`)
          }
        }
        console.log("ok")
      } else {
        process.exit(1)
      }
      return
    }

    const valid_events = ["UserPromptSubmit", "Stop", "Notification"]
    if (!valid_events.includes(expected_event_name)) {
      console.error(`Invalid hook type: ${expected_event_name}`)
      console.error(`Valid hook types: ${valid_events.join(", ")}`)
      process.exit(1)
    }

    // ensure hooks are registered (cc-switch may overwrite settings between hook calls)
    ensure_hooks_registered()
    ensure_hooks_in_ccswitch()

    if (!input_str) {
      // no stdin data (rare), just return
      return
    }

    const data = sanitize_surrogates(JSON.parse(input_str))
    validate_input_data(data, expected_event_name)

    const tracker = new ClaudePromptTracker()

    if (expected_event_name === "UserPromptSubmit") {
      tracker.handle_user_prompt_submit(data)
    } else if (expected_event_name === "Stop") {
      await tracker.handle_stop(data)
    } else if (expected_event_name === "Notification") {
      tracker.handle_notification(data)
    }
  } catch (e) {
    console.error(`Error: ${e.message}`)
    process.exit(1)
  }
}

main()