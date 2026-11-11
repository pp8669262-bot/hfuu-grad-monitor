#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
合肥大学通知监测器
==================
抓取合肥大学官网各栏目，筛出「研究生事务 / 奖学金与资助 / 竞赛与创新创业」
相关通知，去重后生成 Markdown 日报；有新通知时发邮件、写桌面日报，
并可选创建 GitHub Issue（云端模式）。

两种运行方式
------------
1) 本机常驻（近实时，推荐）：
       pythonw monitor.py --loop --local
   默认每 180 秒扫一轮，开机自启由 Windows 计划任务负责。
   邮件用本机 config.json 里的 163 SMTP 授权码直发，凭证不上传云端。

2) 单次运行（GitHub Actions / 手动）：
       python monitor.py                 # 正常一轮，推送 Issue + 邮件
       python monitor.py --dry-run       # 只打印，不写状态、不推送
       python monitor.py --init          # 重建基线（不推送）

命令行参数
----------
    --loop                常驻循环运行
    --interval N          轮询间隔秒数，默认读 config.json 的 interval_seconds（180）
    --local               本地模式：跳过 GitHub Issue，只发邮件 + 写桌面日报
    --no-mail             本轮不发邮件
    --pages N             每个栏目抓几页，默认 2
    --no-summary          不抓取正文摘要
    --dry-run / --init    见上

环境变量
--------
    GITHUB_TOKEN / GITHUB_REPOSITORY   Actions 自动注入，用于建 Issue
    HFUU_PAGES                        默认抓取页数
    HFUU_NO_SUMMARY                   设为 1 关闭摘要
    HFUU_TRUST_ENV                    设为 1 时允许使用系统代理环境变量
    HFUU_LOCAL_OUT                    桌面日报输出目录（默认 D:\\桌面\\合肥大学通知速递）
    HFUU_MAIL_USER / HFUU_MAIL_PASS / HFUU_MAIL_TO
                                      云端 Secrets 注入的 SMTP 配置（覆盖 config.json）
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

SITE = "https://www.hfuu.edu.cn"
CN_TZ = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# ---------------------------------------------------------------------------
# 监测栏目
#   (栏目名称, 列表页 URL, 模式, 默认分类)
#   模式 all    = 该栏目整栏收录（研究生院通知、赛事发布这类本身就是目标内容）
#   模式 filter = 只有标题命中关键词才收录
# ---------------------------------------------------------------------------
SOURCES = [
    ("研究生院 · 通知公告",       f"{SITE}/yjs/yjsjyxw/list.htm",   "all",    "研究生事务"),
    ("研究生院 · 公示公告",       f"{SITE}/yjs/gsgg/list.htm",       "all",    "研究生事务"),
    ("研究生院 · 研招通知",       f"{SITE}/yjs/5767/list.htm",       "all",    "研究生事务"),
    ("研究生院 · 学生工作通知",   f"{SITE}/yjs/3499/list.htm",       "all",    "研究生事务"),
    ("研究生院 · 规章制度",       f"{SITE}/yjs/3497/list.htm",       "filter", "研究生事务"),
    ("研究生院 · 首页",           f"{SITE}/yjs/",                   "filter", "研究生事务"),
    ("生环学院 · 通知公告",       f"{SITE}/swx/1045/list.htm",       "filter", "研究生事务"),
    ("生环学院 · 首页",           f"{SITE}/swx/",                   "filter", "研究生事务"),
    ("创新创业学院 · 赛事发布",   f"{SITE}/cxcy/ssfb/list.htm",      "all",    "竞赛与创新创业"),
    ("创新创业学院 · 首页",       f"{SITE}/cxcy/",                  "filter", "竞赛与创新创业"),
    ("团委 · 首页",               f"{SITE}/tw/",                    "filter", "竞赛与创新创业"),
    ("教务处 · 相关通知",         f"{SITE}/jwc/xgtz/list.htm",       "filter", "研究生事务"),
    ("教务处 · 首页",             f"{SITE}/jwc/",                   "filter", "研究生事务"),
    ("信息公开网 · 首页",         f"{SITE}/xxgk/",                  "filter", "研究生事务"),
]

# 关键词分组：顺序即优先级，一条通知可命中多组，但只归入第一组展示
TOPICS = {
    "奖学金与资助": [
        "奖学金", "奖助", "助学金", "资助", "综测", "综合测评", "评优", "评选",
        "评审", "三助", "励志奖", "困难补助", "绿色通道", "荣誉称号", "奖金额",
    ],
    "竞赛与创新创业": [
        "竞赛", "大赛", "比赛", "挑战杯", "互联网+", "技能竞赛", "参赛", "选拔赛",
        "决赛", "复赛", "创新创业", "创新实践", "创新训练", "大创", "学术论坛",
        "研究生论坛", "创新大赛", "赛项", "获奖",
    ],
    "研究生事务": [
        "研究生", "硕士", "博士", "研招", "复试", "调剂", "开题", "学位论文",
        "导师", "培养方案", "学籍", "推免", "专业学位", "研究生院", "研工部",
        "学位点", "中期检查", "答辩", "学科建设", "招生简章", "专业目录",
    ],
}

# 明显无关的行政/采购类标题，全局排除（避免招标采购刷屏）
EXCLUDE = (
    "招标公告", "采购公告", "成交公示", "中标公告", "比价采购", "询价公告",
    "单一来源", "采购项目", "采购", "招标", "中标", "成交公告", "评审纪要",
    "人才引进", "教职工", "趣味运动会", "运动会", "校历", "高层次人才",
    "预公告", "校园风光", "走访调研", "签约仪式",
)

# 成果报道 / 活动新闻（不是可报名的通知），全局排除
NEWS_EXCLUDE = (
    "斩获", "荣获", "喜获", "佳绩", "勇夺", "告捷", "夺魁", "顺利举办",
    "成功举办", "圆满", "顺利结束", "顺利举行", "顺利开展", "圆满落幕",
    "胜利", "落幕", "系列主题活动", "观影活动", "发表重要研究成果",
    "社会实践活动评选", "闭幕", "开幕", "顺利进行",
)

# 公文体裁词：filter 模式下的标题必须带有其一，滤掉纯新闻/活动稿
NOTICE_HINTS = (
    "通知", "公告", "公示", "简章", "办法", "名单", "申报", "评选", "评审",
    "结果", "安排", "计划", "关于", "目录", "规程", "细则", "意见", "提示",
    "复查", "检查", "须知", "指南", "日程", "报送", "遴选", "认定", "备案",
    "申请表", "倡议书", "截止", "征集", "答辩", "开题",
)

DATE_RE = re.compile(r"(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})")
BODY_SELECTORS = [
    "div.wp_articlecontent", "div.articlecontent", "div.Article_Content",
    "div.article_content", "div.v_news_content", "div.wrapper", "div.content",
]

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state" / "seen.json"
REPORT_DIR = ROOT / "reports"
CONFIG_FILE = ROOT / "config.json"
LOG_FILE = ROOT / "daemon.log"
LOCAL_OUT = Path(os.environ.get("HFUU_LOCAL_OUT", r"D:\桌面\合肥大学通知速递"))

DEFAULT_CONFIG = {
    "interval_seconds": 180,
    "mail": {
        "enabled": True,
        "smtp_host": "smtp.163.com",
        "smtp_port": 465,
        "ssl": True,
        "user": "qwe5200202@163.com",
        "auth_code": "",
        "to": ["qwe5200202@163.com"],
        "from_name": "合大通知速递",
    },
}

MAIL_CSS = (
    "body{font-family:'Microsoft YaHei',Arial,sans-serif;line-height:1.7;"
    "color:#24292f;max-width:760px;margin:0 auto;padding:18px}"
    "h1{font-size:22px;border-bottom:3px solid #0969da;padding-bottom:10px}"
    "h2{font-size:18px;color:#0969da;margin-top:28px}"
    "h3{font-size:16px;margin:18px 0 6px}"
    "h3 a{color:#0969da;text-decoration:none}"
    "li,p,blockquote{font-size:14px}"
    "blockquote{background:#f6f8fa;border-left:4px solid #d0d7de;"
    "margin:12px 0;padding:8px 14px;color:#57606a}"
    "hr{border:0;border-top:1px solid #d8dee4;margin:22px 0}"
    "a{color:#0969da}"
)


def log(msg: str) -> None:
    line = f"[{datetime.now(CN_TZ):%Y-%m-%d %H:%M:%S}] {msg}"
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1_500_000:
            LOG_FILE.write_text("", encoding="utf-8")  # 简单轮转，避免日志无限增长
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        print(f"[monitor] {msg}", flush=True)
    except Exception:
        pass  # pythonw 无控制台时 stdout 可能不可用


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if not CONFIG_FILE.exists():
        try:
            CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
            log("已生成配置模板 config.json，填入 mail.auth_code 后即可发邮件")
        except Exception as exc:
            log(f"配置模板写入失败: {type(exc).__name__}: {exc}")
    else:
        try:
            d = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            cfg.update({k: v for k, v in d.items() if k != "mail"})
            cfg["mail"].update(d.get("mail", {}))
        except Exception as exc:
            log(f"配置读取失败，使用默认值: {type(exc).__name__}: {exc}")
    # 云端 Secrets 覆盖本地配置
    if os.environ.get("HFUU_MAIL_USER"):
        cfg["mail"]["user"] = os.environ["HFUU_MAIL_USER"]
    if os.environ.get("HFUU_MAIL_PASS"):
        cfg["mail"]["auth_code"] = os.environ["HFUU_MAIL_PASS"]
    if os.environ.get("HFUU_MAIL_TO"):
        cfg["mail"]["to"] = [x.strip() for x in os.environ["HFUU_MAIL_TO"].split(",") if x.strip()]
    return cfg


def make_session() -> requests.Session:
    s = requests.Session()
    if os.environ.get("HFUU_TRUST_ENV") != "1":
        s.trust_env = False  # 忽略系统代理，避免本机代理配置污染抓取
    s.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    return s


def fetch(session: requests.Session, url: str, retries: int = 2) -> str | None:
    for i in range(retries + 1):
        try:
            r = session.get(url, timeout=20)
            r.raise_for_status()
            r.encoding = "utf-8"  # 站点未在响应头声明 charset，必须强制 UTF-8
            return r.text
        except Exception as exc:
            if i == retries:
                log(f"抓取失败 {url} -> {type(exc).__name__}: {exc}")
                return None
            time.sleep(1.5 * (i + 1))
    return None


def page_urls(list_url: str, pages: int) -> list[str]:
    """Webplus 分页规则：list.htm / list2.htm / list3.htm ..."""
    if pages <= 1:
        return [list_url]
    m = re.match(r"^(.*/)list\.htm$", list_url)
    if not m:
        return [list_url]
    return [list_url] + [f"{m.group(1)}list{i}.htm" for i in range(2, pages + 1)]


def classify(title: str, default_topic: str) -> tuple[str, list[str]]:
    tags = [name for name, words in TOPICS.items() if any(w in title for w in words)]
    return (tags[0] if tags else default_topic), tags


def is_relevant(title: str, mode: str) -> bool:
    if any(x in title for x in EXCLUDE):
        return False
    if any(x in title for x in NEWS_EXCLUDE):
        return False
    if mode == "all":
        return True  # 通知公告 / 赛事发布等整栏本身就是目标内容
    if not any(w in title for words in TOPICS.values() for w in words):
        return False
    return any(h in title for h in NOTICE_HINTS)  # 必须是公文，不是新闻稿


def parse_list(html_text: str, base_url: str, source: str, mode: str,
               default_topic: str) -> list[dict]:
    soup = BeautifulSoup(html_text, "html.parser")
    out: list[dict] = []
    seen_href: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "page.htm" not in href or href in seen_href:
            continue
        seen_href.add(href)
        # 列表页通常把完整标题放在 title 属性里（正文文字可能被截断）
        title = (a.get("title") or a.get_text(" ", strip=True) or "").strip()
        title = re.sub(r"\s+", " ", title)
        if len(title) < 6:
            continue
        url = urljoin(base_url, href)

        # 向上找最近的 li / tr / div，取出发布日期
        date = ""
        node = a
        for _ in range(5):
            node = node.parent
            if node is None or node.name in ("body", "html"):
                break
            m = DATE_RE.search(node.get_text(" ", strip=True))
            if m:
                date = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
                break

        if not is_relevant(title, mode):
            continue

        primary, tags = classify(title, default_topic)
        out.append({
            "id": hashlib.sha1(url.encode("utf-8")).hexdigest()[:16],
            "title": title, "date": date, "url": url,
            "source": source, "topic": primary, "tags": tags,
        })
    return out


def fetch_summary(session: requests.Session, url: str) -> str:
    html_text = fetch(session, url, retries=1)
    if not html_text:
        return ""
    soup = BeautifulSoup(html_text, "html.parser")
    text = ""
    for sel in BODY_SELECTORS:
        node = soup.select_one(sel)
        if node and len(node.get_text(strip=True)) > 80:
            text = node.get_text(" ", strip=True)
            break
    if not text:
        parts = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        text = " ".join(p for p in parts if len(p) > 15)
    text = re.sub(r"\s+", " ", text)
    # 去掉页头「发布者 / 发布时间 / 浏览次数」等噪声
    text = re.sub(r"^.*?发布时间[:：]\s*20\d{2}[-/]\d{1,2}[-/]\d{1,2}\s*"
                  r"(浏览次数[:：]\s*\d+\s*)?", "", text)
    return text[:150].strip()


def crawl(session: requests.Session, pages: int) -> list[dict]:
    all_items: list[dict] = []
    for name, url, mode, default_topic in SOURCES:
        found: list[dict] = []
        for purl in page_urls(url, pages):
            html_text = fetch(session, purl)
            if html_text:
                found.extend(parse_list(html_text, purl, name, mode, default_topic))
        # 同栏目跨页去重
        uniq = {it["id"]: it for it in found}
        all_items.extend(uniq.values())
        if uniq:
            log(f"{name}: {len(uniq)} 条")
    # 全局按 URL 去重，保留第一次出现的栏目
    merged: dict[str, dict] = {}
    for it in all_items:
        merged.setdefault(it["id"], it)
    return list(merged.values())


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            log(f"状态文件损坏，将重建基线: {exc}")
    return {"version": 1, "updated_at": "", "items": {}}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # 控制体积，最多保留最近 6000 条
    items = state["items"]
    if len(items) > 6000:
        ordered = sorted(items.items(), key=lambda kv: kv[1].get("first_seen", ""), reverse=True)
        state["items"] = dict(ordered[:6000])
    state["updated_at"] = datetime.now(CN_TZ).isoformat(timespec="seconds")
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def build_report(new_items: list[dict], baseline: bool, total: int, now: datetime) -> str:
    stamp = now.strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# 合肥大学通知速递 · {stamp}",
        "",
        f"本次扫描 {total} 条在榜通知，"
        + (f"新增 **{len(new_items)}** 条。" if not baseline
           else f"已建立基线（{len(new_items)} 条历史通知），本次不推送。"),
        "",
        "> 关注范围：研究生事务 / 奖学金与资助 / 竞赛与创新创业。"
        "数据来自合肥大学官网公开栏目，按标题关键词筛选并去重。",
        "",
    ]
    if baseline:
        lines += ["---", "", "## 基线快照（按栏目，仅存档不推送）", ""]
        groups: dict[str, list[dict]] = {}
        for it in new_items:
            groups.setdefault(it["source"], []).append(it)
        for source, items in groups.items():
            lines.append(f"**{source}**（{len(items)} 条）")
            lines.append("")
            for it in items[:8]:
                lines.append(f"- {it['date'] or '日期未标注'} · {it['title']}")
            if len(items) > 8:
                lines.append(f"- … 另有 {len(items) - 8} 条")
            lines.append("")
        return "\n".join(lines)

    lines += ["---", ""]
    if not new_items:
        lines += ["本次无新增相关通知。", ""]
        return "\n".join(lines)

    by_topic: dict[str, list[dict]] = {}
    for it in new_items:
        by_topic.setdefault(it["topic"], []).append(it)
    for topic in TOPICS:
        items = by_topic.get(topic, [])
        if not items:
            continue
        lines += [f"## {topic}（{len(items)} 条）", ""]
        for it in sorted(items, key=lambda x: x["date"], reverse=True):
            lines.append(f"### [{it['title']}]({it['url']})")
            lines.append("")
            meta = f"- 发布日期：{it['date'] or '未标注'}　|　来源：{it['source']}"
            lines.append(meta)
            if len(it["tags"]) > 1:
                lines.append(f"- 标签：{'、'.join(it['tags'])}")
            if it.get("summary"):
                lines.append(f"- 摘要：{it['summary']}…")
            lines.append("")
    lines += [
        "---", "",
        "点标题即可跳转官网原文。如需增减栏目或关键词，编辑 `monitor.py` 顶部的 `SOURCES` / `TOPICS`。",
        "",
    ]
    return "\n".join(lines)


def linkify(text: str) -> str:
    safe = html.escape(text, quote=False)
    return re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)",
                  r'<a href="\2">\1</a>', safe)


def md_to_html(md: str) -> str:
    parts: list[str] = []
    for raw in md.splitlines():
        s = raw.strip()
        if not s:
            continue
        if s.startswith("### "):
            parts.append(f"<h3>{linkify(s[4:])}</h3>")
        elif s.startswith("## "):
            parts.append(f"<h2>{linkify(s[3:])}</h2>")
        elif s.startswith("# "):
            parts.append(f"<h1>{linkify(s[2:])}</h1>")
        elif s.startswith("> "):
            parts.append(f"<blockquote>{linkify(s[2:])}</blockquote>")
        elif s.startswith("- "):
            parts.append(f"<li>{linkify(s[2:])}</li>")
        elif s == "---":
            parts.append("<hr>")
        else:
            parts.append(f"<p>{linkify(s)}</p>")
    return (f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<style>{MAIL_CSS}</style></head><body>" + "\n".join(parts)
            + "</body></html>")


def send_mail(cfg: dict, subject: str, report_md: str) -> bool:
    m = cfg["mail"]
    if not m.get("enabled", True):
        log("邮件推送已在配置中关闭")
        return False
    if not m.get("auth_code"):
        log("未发送邮件：config.json 里 mail.auth_code 还是空的（需填 163 SMTP 授权码）")
        return False
    try:
        msg = MIMEText(md_to_html(report_md), "html", "utf-8")
        msg["From"] = formataddr((str(Header(m.get("from_name", "合大通知速递"), "utf-8")),
                                  m["user"]))
        msg["To"] = ", ".join(m["to"])
        msg["Subject"] = Header(subject, "utf-8")
        host, port = m["smtp_host"], int(m["smtp_port"])
        if m.get("ssl", True):
            server = smtplib.SMTP_SSL(host, port, timeout=25)
        else:
            server = smtplib.SMTP(host, port, timeout=25)
            server.starttls()
        server.login(m["user"], m["auth_code"])
        server.sendmail(m["user"], m["to"], msg.as_string())
        server.quit()
        log(f"邮件已发送至 {', '.join(m['to'])}｜主题：{subject}")
        return True
    except Exception as exc:
        log(f"邮件发送失败: {type(exc).__name__}: {exc}")
        return False


def write_local_report(report: str, now: datetime, new_count: int) -> None:
    try:
        LOCAL_OUT.mkdir(parents=True, exist_ok=True)
        (LOCAL_OUT / "日报").mkdir(exist_ok=True)
        (LOCAL_OUT / "最新速递.md").write_text(report, encoding="utf-8")
        (LOCAL_OUT / "日报" / f"{now:%Y-%m-%d_%H%M}.md").write_text(report, encoding="utf-8")
        if new_count:
            (LOCAL_OUT / "最新提醒.md").write_text(report, encoding="utf-8")
        log(f"桌面日报已写入 {LOCAL_OUT}")
    except Exception as exc:
        log(f"桌面日报写入失败: {type(exc).__name__}: {exc}")


def create_issue(title: str, body: str) -> bool:
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        log("未检测到 GITHUB_TOKEN / GITHUB_REPOSITORY，跳过 Issue 推送")
        return False
    try:
        s = requests.Session()
        if os.environ.get("HFUU_TRUST_ENV") != "1":
            s.trust_env = False
        r = s.post(
            f"https://api.github.com/repos/{repo}/issues",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "hfuu-grad-monitor",
            },
            json={"title": title, "body": body},
            timeout=25,
        )
        if r.status_code == 201:
            log(f"Issue 已创建: {r.json().get('html_url')}")
            return True
        log(f"Issue 创建失败: HTTP {r.status_code} {r.text[:200]}")
    except Exception as exc:
        log(f"Issue 创建异常: {type(exc).__name__}: {exc}")
    return False


def run_once(args, cfg: dict, state: dict) -> dict:
    now = datetime.now(CN_TZ)
    session = make_session()
    baseline = args.init or not state["items"]

    log(f"开始扫描（{now:%Y-%m-%d %H:%M} 北京时间），每栏目 {args.pages} 页")
    items = crawl(session, args.pages)
    log(f"合计 {len(items)} 条相关通知")

    new_items = [it for it in items if it["id"] not in state["items"]]
    log(f"新增 {len(new_items)} 条，模式：{'基线' if baseline else '正常'}")

    need_summary = (not baseline and not args.no_summary and new_items
                    and os.environ.get("HFUU_NO_SUMMARY") != "1")
    if need_summary:
        for it in new_items[:20]:
            it["summary"] = fetch_summary(session, it["url"])
            time.sleep(0.3)

    report = build_report(new_items, baseline, len(items), now)

    if args.dry_run:
        try:
            print("\n" + "=" * 60)
            print(report[:6000])
            print("=" * 60)
        except Exception:
            pass
        return state

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"{now:%Y-%m-%d}-{now:%H%M}.md"
    (REPORT_DIR / fname).write_text(report, encoding="utf-8")
    (REPORT_DIR / "latest.md").write_text(report, encoding="utf-8")

    # 本机常驻模式：桌面日报 + 邮件；非本地模式额外发 GitHub Issue
    if args.local:
        write_local_report(report, now, 0 if baseline else len(new_items))

    if not baseline and new_items:
        counts: dict[str, int] = {}
        for it in new_items:
            counts[it["topic"]] = counts.get(it["topic"], 0) + 1
        detail = " / ".join(f"{k} {v}" for k, v in counts.items())
        subject = f"[合大通知速递] {now:%m-%d %H:%M} 新增 {len(new_items)} 条（{detail}）"
        if not args.no_mail:
            send_mail(cfg, subject, report)
        if not args.local:
            create_issue(subject, report + f"\n\n> 自动生成 · {fname}\n")
    elif baseline:
        log("基线已建立，本次不推送（下次运行起只提醒新增通知）")

    stamp = now.isoformat(timespec="seconds")
    for it in items:
        state["items"].setdefault(it["id"], {
            "title": it["title"], "date": it["date"], "url": it["url"],
            "source": it["source"], "first_seen": stamp,
        })
    save_state(state)
    log("本轮完成，状态已保存")
    return state


def main() -> int:
    ap = argparse.ArgumentParser(description="合肥大学研究生/奖学金/竞赛通知监测器")
    ap.add_argument("--loop", action="store_true", help="常驻循环，按间隔持续监测")
    ap.add_argument("--interval", type=int, default=0, help="轮询间隔秒数，默认 180")
    ap.add_argument("--local", action="store_true", help="本机模式：写桌面日报、跳过 GitHub Issue")
    ap.add_argument("--no-mail", action="store_true", help="本轮不发邮件")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写状态文件、不推送")
    ap.add_argument("--init", action="store_true", help="强制重建基线，不推送")
    ap.add_argument("--pages", type=int, default=int(os.environ.get("HFUU_PAGES", "2")),
                    help="每个栏目抓取页数，默认 2")
    ap.add_argument("--no-summary", action="store_true", help="不抓取正文摘要")
    args = ap.parse_args()

    cfg = load_config()
    state = load_state()

    if not args.loop:
        run_once(args, cfg, state)
        return 0

    interval = args.interval or int(cfg.get("interval_seconds", 180))
    if interval < 60:
        interval = 60  # 保护官网，避免过于频繁请求
    log(f"常驻监测启动：每 {interval} 秒一轮 | 收件 {', '.join(cfg['mail']['to'])} "
        f"| 输出 {LOCAL_OUT}")
    while True:
        try:
            state = run_once(args, cfg, state)
        except Exception as exc:
            log(f"本轮异常，{interval} 秒后重试: {type(exc).__name__}: {exc}")
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
