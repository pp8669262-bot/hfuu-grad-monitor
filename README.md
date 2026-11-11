# 合肥大学通知监测器

自动盯合肥大学官网，把**研究生事务、奖学金与资助、竞赛与创新创业**相关的新通知整理成日报，
发现新通知就发到邮箱 **qwe5200202@163.com**；没有新通知不打扰。

## 现在怎么跑（近实时，不是固定时间）

本机有一个**常驻后台进程**，默认每 **180 秒（3 分钟）**扫一轮，开机登录后自动启动，
不依赖 GitHub、也不需要定时点：

```
本机常驻进程（Windows 计划任务 HFUU-Grad-Monitor-Realtime）
        ↓ 每 180 秒
抓取 14 个官网栏目（每栏目 2 页）
        ↓
关键词筛选 + 排除招标采购 / 活动新闻
        ↓
与 state/seen.json 比对去重
        ↓
日报写到 D:\桌面\合肥大学通知速递\最新速递.md（并存档到 日报\）
        ↓
有新增 → 另写 最新提醒.md + 发 HTML 邮件到 qwe5200202@163.com
```

> GitHub Actions 做不到真·实时（最短 5 分钟一跑，且经常排队延迟），所以云端那份
> **已取消固定定时**，只保留手动触发作为备份。本机这份才是主渠道。

常用管理命令（PowerShell）：

```powershell
Get-ScheduledTask -TaskName 'HFUU-Grad-Monitor-Realtime'   # 看状态
Stop-ScheduledTask -TaskName 'HFUU-Grad-Monitor-Realtime'  # 暂停
Start-ScheduledTask -TaskName 'HFUU-Grad-Monitor-Realtime' # 启动
```

日志在项目目录 `daemon.log`（超过 1.5 MB 自动清空重来）。

## 邮箱配置（已完成）

程序用 **163 邮箱 SMTP** 给自己发信，收件与发件都是 `qwe5200202@163.com`。
SMTP 不用邮箱登录密码，要用**授权码**。

**当前状态（2026-11-11 已配置并验证）**：网页端 `设置 → POP3/SMTP/IMAP` 里
**IMAP/SMTP 服务已开启**；授权码已填入本机 `config.json`；
`smtp.163.com:465 SSL` 实测发信成功，测试邮件已送达 `qwe5200202@163.com`；
常驻进程已重启并加载新配置。之后只有抓到**新通知**才会发邮件。

以后若授权码失效或换邮箱，按下面重来一次：

1. 网页登录 163 邮箱 → 顶部 **设置** → **POP3/SMTP/IMAP**。
2. 确认 **IMAP/SMTP 服务** 显示「已开启」；没开就点开启（要短信验证）。
3. 点 **新增授权密码**，复制那串 16 位左右的字符串（只显示一次）。
4. 打开 `config.json`，把 `mail.auth_code` 换成新码：

```json
{
  "interval_seconds": 180,
  "mail": {
    "enabled": true,
    "smtp_host": "smtp.163.com",
    "smtp_port": 465,
    "ssl": true,
    "user": "qwe5200202@163.com",
    "auth_code": "<16位授权码>",
    "to": ["qwe5200202@163.com"],
    "from_name": "合大通知速递"
  }
}
```

5. **重启一次常驻进程**让它读到新配置：
   `Stop-ScheduledTask` 然后 `Start-ScheduledTask`（任务名见上）。

`config.json`、`daemon.log`、`.browser-profile/` 都在 `.gitignore` 里排除，
**授权码和邮箱登录态不会上传 GitHub**；分发用的 zip 备份也不含这些文件。
改轮询频率就改 `interval_seconds`（程序有 60 秒下限，别把学校官网刷太狠）。

授权码没填时程序照常运行，只是不发邮件，日志会写
`未发送邮件：config.json 里 mail.auth_code 还是空的`，日报和 `最新提醒.md` 仍会照常生成。

邮件是带样式的 HTML，主题形如
`[合大通知速递] 11-10 15:20 新增 3 条（奖学金与资助 1 / 竞赛与创新创业 2）`，
每条通知标题可直接点开官网原文。

## 监测栏目

| 类别 | 栏目 |
| --- | --- |
| 研究生院 | 通知公告、公示公告、研招通知、学生工作通知、规章制度、首页 |
| 生命健康与环境工程学院 | 通知公告、首页 |
| 创新创业学院 | 赛事发布、首页 |
| 校团委 | 首页 |
| 教务处 | 相关通知、首页 |
| 学校 | 信息公开网首页 |

栏目清单在 `monitor.py` 顶部的 `SOURCES`，关键词在 `TOPICS`，屏蔽词在 `EXCLUDE` / `NEWS_EXCLUDE`。
想加栏目：往 `SOURCES` 里加一行 `(名称, URL, "all"或"filter", 默认分类)`。

- `all`：整栏都算目标内容（如研究生院通知公告）
- `filter`：标题命中关键词、且带公文体裁词（通知/公示/申报…）才收录，滤掉纯新闻稿

## 首次运行说明

`state/seen.json` 里已存好 88 条历史通知作为**基线**，所以不会一上来就把历史通知全发给你。
之后只推送新增。想重建基线：`python monitor.py --init --local`。

## 本地手动运行

```bash
pip install -r requirements.txt
python monitor.py --dry-run --local      # 试跑，只打印
python monitor.py --local --no-mail      # 正式跑一轮，不发邮件
python monitor.py --loop --local         # 前台常驻（调试用，正式用计划任务）
```

参数：`--interval N`（覆盖配置里的间隔）、`--pages 3`（多抓一页）、
`--no-summary`（不抓正文摘要）、`--no-mail`、`--init`、`--dry-run`。

## 云端备份（GitHub 仓库）

仓库 `pp8669262-bot/hfuu-grad-monitor` 里有一份同样代码和手动工作流。
如需在云端也能发邮件，在仓库 Settings → Secrets 加三个变量：
`MAIL_USER` = `qwe5200202@163.com`、`MAIL_PASS` = 163 授权码、`MAIL_TO` = `qwe5200202@163.com`，
然后在 Actions 页面手动 **Run workflow**。

## 几点注意

- 官网使用 Webplus 建站系统，列表页 `list.htm` / `list2.htm`，详情页 `page.htm`；
  若学校改版导致解析失效，改 `parse_list()` 里的选择器。
- 响应头未声明 charset，代码强制按 UTF-8 解码；默认忽略系统代理（`HFUU_TRUST_ENV=1` 可开启）。
- 抓取是低频的只读请求，每轮约 28 个页面、3 分钟一轮，详情页只在有新增时抓最多 20 条。
- 本机与云端各自维护去重状态，两边同时跑会各自判断"新增"。所以云端不设定时，避免重复提醒。
