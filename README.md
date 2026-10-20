# Weavlum · 黑旗 SRD 局域网跑团平台

<h3 align="center">Developed with DeepSeek</h3>

一个中文、自托管的桌面角色扮演（TRPG）平台：**AI 游戏主持人（GM）+ 服务器权威规则引擎 + 局域网多人联机**。
规则体系基于 **Black Flag Roleplaying（黑旗 SRD，ORC 许可）**：13 职业（含机械师）、6 血统 + 9 传承、
3 背景 + 30 天赋、316 条法术（0-9 环）、370 件物品（含魔法物品）、360 只怪物。

## 特性一览

- **服务器是唯一裁判**：所有数值与规则计算（属性/AC/HP/法术位/幸运/负重/遭遇）都在服务端引擎完成，
  前端只渲染，AI 只能通过工具调用请求计算——AI、人类 GM、玩家共用同一套动作层。
- **AI GM**：可配置任意 OpenAI 兼容 LLM；自动开场、叙事、掷骰、配怪（CR 粗估口径）、记账；
  无 AI 时也可纯人工 GM 主持。
- **GM 席位自助接任**：欢迎页一键接任（先到先得）；GM 拥有含剧透的「GM 秘闻」与控制台，与 AI 同权。
- **完整规则覆盖**：1-20 级、提升（Improvement 三选一）、子职业（3 级）、英雄/史诗恩赐（10/20 级）、
  幸运系统、30 天赋（8 条数值型机制化）、四级货币、负重、战术板（缩放/平移/5 尺网格）。
- **局域网即开即用**：`./start.sh`（Linux/WSL）或双击 `start.cmd`（Windows），浏览器加入；玩家颜色与头像、
  悬停图鉴（怪物/法术/技能/种族/天赋/物品全文说明）。

## 快速开始

```bash
# Linux / WSL
./start.sh

# Windows（双击或命令行）
start.cmd
```

启动后按终端提示把局域网网址发给同桌玩家；主机用 `localhost`。
首次使用 Windows 若被防火墙拦截，以管理员 PowerShell 运行 `scripts/windows_firewall.ps1 -Port 8000`。
管理页地址（含主机令牌）在启动终端打印。

服务器实际跑在 WSL 里，**关闭 start.cmd 窗口不一定会结束它**。停止方式：

```bash
./stop.sh            # Linux / WSL；--status 查看状态
```

Windows 双击 `stop.cmd`（或 `start.cmd --stop`），`stop.cmd --status` 查看状态。
已有实例时 `start.sh` / `start.cmd` 会拒绝重复启动，避免新实例悄悄换到 8001 端口。

## 外网联机（可选，独立脚本）

不在同一局域网时，可用 Cloudflare 临时隧道（无需账号/域名/路由器配置）：

```bash
./scripts/tunnel.sh          # Linux / WSL（先启动服务器；默认转发 8000）
./scripts/tunnel.sh --status # 查看隧道状态
./scripts/tunnel.sh --stop   # 主动结束后台隧道
```

Windows 双击 `scripts/tunnel.cmd`（或 `tunnel.cmd 8000`），
`tunnel.cmd --status` 查看状态，`tunnel.cmd --stop` 主动结束隧道。首次运行会自动下载
cloudflared 到 `data/bin/`（约 53MB，GitHub 不通时自动走镜像；下载有进度条，单源最长 90 秒）。窗口中出现
`https://<随机子域>.trycloudflare.com` 即为公网网址，发给队友即可。

- 网址**每次启动都会变化**；关闭隧道窗口即断开
- 公网暴露提示：平台无账号体系，请只发给队友，不要长期公开挂机

## 开发

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python run.py            # 普通启动
.venv/bin/python run.py --debug    # 调试模式（JSONL 诊断日志）
.venv/bin/python -m unittest discover -s tests -t .   # 160 项测试
```

数据构建（规则数据在 `data/srd/`，由脚本从中文译稿生成，可一键重建）：

```bash
.venv/bin/python scripts/build_bfrd.py     # 从 markdown/ 译稿解析
.venv/bin/python scripts/check_data.py     # 校验（0 错 0 警）
.venv/bin/python scripts/install_bfrd.py   # 安装到 data/srd/
```

## 许可

- **平台代码**：GNU Affero 通用公共许可证第 3 版或更高版本（AGPL-3.0-or-later，见 `LICENSE`）。
  本程序是自由软件：你可以再分发和/或修改它，但需遵守 AGPL。
  **通过网络与本程序交互的用户有权获得 Corresponding Source（对应源码）**；
  自托管到公网时请设置环境变量 `WEAVLUM_SOURCE_URL` 指向源码仓库，`/credits` 页会展示该链接。
- **规则数据**：来自《黑旗角色扮演参考文档 v0.2》，
  **Black Flag Roleplaying Reference Document v0.2, © Open Design LLC d/b/a Kobold Press**，
  依据 ORC 许可证授权（存档编号 TX 9-307-067，<https://koboldpress.com/orclicense>）。
  ORC 许可证全文见仓库根目录的 `LICENSE-ORC`。
  其中部分材料取自 Wizards of the Coast LLC 的 SRD 5.1（CC-BY-4.0，
  <https://creativecommons.org/licenses/by/4.0/legalcode>）。
- 平台代码与规则数据是相互独立的作品：AGPL 不改变 `data/srd/` 等规则数据的 ORC 授权。
- 应用内「许可与致谢」页（`/credits`）载有 ORC 声明、署名与源码获取方式。

---

<p><sub>DeepSeek 为开发辅助工具；本项目与 DeepSeek 官方无隶属、合作或背书关系。DeepSeek 名称归其权利人所有。</sub></p>
