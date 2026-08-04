# Hermes Reach

中文 | [English](README_EN.md)

Hermes Reach 是 [Hermes Agent](https://github.com/NousResearch/hermes-agent)
的只读检索插件。它向 Hermes 注册五个工具，平台调用、结果整理和后端选择由固定在
指定提交的 [Agent-Reach fork](https://github.com/izumi0uu/Agent-Reach) 负责。

当前源码版本是 `0.1.0a2`，处于 Pre-Alpha 阶段。建议先在本地试用。

## 快速开始

需要 Python 3.11 至 3.13、`uv`、Git 和 Hermes Agent 0.19.x。

```bash
git clone https://github.com/izumi0uu/hermes-reach.git
cd hermes-reach
uv sync --locked --all-groups

uv run hermes plugins enable reach --no-allow-tool-override
```

启用配置在下一个 Hermes 进程生效。启动新进程后先检查实际能力：

```bash
uv run hermes reach status --json
uv run hermes reach sources --json
uv run hermes reach doctor --json
```

启动 Hermes，只加载 Reach 工具和 `reach:agent-reach` 技能：

```bash
uv run hermes \
  --skills reach:agent-reach \
  --toolsets reach
```

然后直接描述任务，例如：

```text
搜索 Bilibili 上关于 Rust 异步运行时的视频，返回最相关的 5 条。
```

```text
读取这个 YouTube 视频的中文字幕：https://www.youtube.com/watch?v=VIDEO_ID
```

`doctor` 默认不联网。`uv run hermes reach doctor --upstream` 会额外运行
受限且脱敏的 Agent-Reach 检查。

## 可用能力

插件定义了 63 项只读操作。目前实现了 34 项，其中 33 项交给 Agent-Reach fork
执行；YouTube 评论读取已经定义请求格式，但还没有接入后端。

| 启用条件 | 来源 | 操作 |
| --- | --- | --- |
| 默认本地可用 | RSS/Atom | `read.feed`、`browse.entries` |
| 默认本地可用 | Bilibili | `search.videos`、`read.video`、`browse.hot`、`browse.rank` |
| 默认本地可用 | YouTube | `search.videos`、`read.video`、`read.subtitles` |
| 默认本地可用 | V2EX | `browse.hot`、`browse.node_topics`、`read.topic`、`read.user` |
| 配置并校验 Exa 所需文件 | Exa | `search.web`、`search.code` |
| 配对并授权 Connector | Reddit | 7 项搜索、读取和浏览操作 |
| 配对并授权 Connector | Facebook | 4 项搜索、读取和浏览操作 |
| 配对并授权 Connector | Instagram | 4 项搜索、读取和浏览操作 |
| 配对并授权 Connector | Twitter/X | `search.posts` |
| 配对并授权 Connector | 小红书 | `search.notes` |
| 配对并授权 Connector | 雪球 | `search.stocks` |
| 当前不可用（未绑定） | YouTube | `read.comments` |
| 当前不可用 | Web、GitHub、LinkedIn、小宇宙及目录中的其他操作 | 没有通过审核的执行代码 |

每个环境的结果以 `reach status` 为准。即使系统中已有所需命令、Cookie 或 API key，
对应来源也不会自动变为 `available`。

## 五个工具

| 工具 | 用途 |
| --- | --- |
| `reach_status` | 查询来源、操作和当前可用性 |
| `reach_search` | 搜索 1 到 5 个明确来源 |
| `reach_read` | 读取一个明确目标 |
| `reach_browse` | 浏览来源原生集合 |
| `reach_transcribe` | 转写受支持的媒体；当前没有默认可用的转写操作 |

目录只包含读取操作。插件不提供发布、评论、点赞、关注或修改账号的能力。

## 安装公开预发布版

[GitHub Releases](https://github.com/izumi0uu/hermes-reach/releases) 是当前公开
发布通道。`v0.1.0a1` 还没有这 33 项操作；如果发布页还没有
`v0.1.0a2`，请按上面的步骤从源码安装。发布工作流上传 wheel、sdist 和
`SHA256SUMS`。发布验收只检查这三个文件，不包括 GitHub 自动生成的
`Source code (zip)` 和 `Source code (tar.gz)`。

```bash
RELEASE_TAG="$(gh release list \
  --repo izumi0uu/hermes-reach \
  --exclude-drafts \
  --limit 1 \
  --json tagName \
  --jq '.[0].tagName')"
test -n "$RELEASE_TAG"
RELEASE_VERSION="${RELEASE_TAG#v}"
RELEASE_DIR="hermes-reach-${RELEASE_TAG}"

gh release download "$RELEASE_TAG" \
  --repo izumi0uu/hermes-reach \
  --dir "$RELEASE_DIR"

gh attestation verify \
  "$RELEASE_DIR/hermes_reach-${RELEASE_VERSION}-py3-none-any.whl" \
  --repo izumi0uu/hermes-reach \
  --signer-workflow izumi0uu/hermes-reach/.github/workflows/release.yml

gh attestation verify \
  "$RELEASE_DIR/hermes_reach-${RELEASE_VERSION}.tar.gz" \
  --repo izumi0uu/hermes-reach \
  --signer-workflow izumi0uu/hermes-reach/.github/workflows/release.yml

cd "$RELEASE_DIR"
shasum -a 256 --check SHA256SUMS
cd ..
```

GNU/Linux 使用 `sha256sum --check SHA256SUMS`。`SHA256SUMS` 本身不是
attestation subject，只用于确认下载后的文件没有变化。

将 wheel 安装到 Hermes 实际使用的 Python 环境：

```bash
HERMES_PYTHON=/absolute/path/to/hermes-environment/bin/python
HERMES_BIN=/absolute/path/to/hermes-environment/bin/hermes

uv pip install \
  --python "$HERMES_PYTHON" \
  "$RELEASE_DIR/hermes_reach-${RELEASE_VERSION}-py3-none-any.whl"
uv pip check --python "$HERMES_PYTHON"

"$HERMES_BIN" plugins enable reach --no-allow-tool-override
```

## 配置 Exa

Exa 不使用 API key。Hermes 不会从 PATH、npm 或编辑器配置中自动寻找
Node 和 mcporter。启动 Hermes 前必须明确提供下面七项配置：

```bash
export HERMES_REACH_EXA_NODE_EXECUTABLE=/absolute/path/to/node
export HERMES_REACH_EXA_NODE_SHA256="<64-lowercase-hex>"
export HERMES_REACH_EXA_MCPORTER_ROOT=/absolute/path/to/mcporter
export HERMES_REACH_EXA_MCPORTER_CLI=/absolute/path/to/mcporter/dist/cli.js
export HERMES_REACH_EXA_MCPORTER_TREE_SHA256="<64-lowercase-hex>"
export HERMES_REACH_EXA_CONFIG_PATH=/absolute/path/to/sterile-config.json
export HERMES_REACH_EXA_CONFIG_SHA256="<64-lowercase-hex>"
```

这七项必须齐全且摘要匹配，否则 Exa 保持 `setup_required`。`search.web` 和
`search.code` 分别调用固定方法，不能互相替代。配置细节见
[Exa 后端决策](docs/agent-reach-decisions/exa-mcporter-1.5.0.md)。

## 在可信设备运行 Connector

Reddit、Facebook、Instagram、Twitter、小红书和雪球默认不在 VPS 执行。
Connector 在你的电脑或其他可信设备上持有浏览器会话、Cookie 和 Bitwarden
访问能力。平台凭据不会写入 VPS；VPS 会保存设备密钥、配对记录、能力快照、授权记录
和回执账本。

先在可信设备和 VPS 上分别完成“快速开始”中的安装和启用。两台机器都要把
`HERMES_REACH_CONNECTOR_HOST` 设为可信设备的可达私有 IP。

初始化可信设备并启动前台服务：

```bash
export HERMES_REACH_CONNECTOR_HOST="<trusted-device-private-ip>"

uv run hermes reach connector init \
  --role connector \
  --state-directory /absolute/connector-state

uv run hermes reach connector serve \
  --state-directory /absolute/connector-state \
  --bind "$HERMES_REACH_CONNECTOR_HOST" \
  --port 8765 \
  --opencli-social-node /absolute/path/to/node \
  --opencli-social-root /absolute/opencli-production-prefix \
  --opencli-social-cli /absolute/opencli-production-prefix/node_modules/@jackwener/opencli/dist/src/main.js \
  --opencli-social-session-home /absolute/trusted-session-home
```

四个 `--opencli-social-*` 参数必须全部提供或全部省略。只启用雪球时使用：

```bash
uv run hermes reach connector serve \
  --state-directory /absolute/connector-state \
  --bind "$HERMES_REACH_CONNECTOR_HOST" \
  --port 8765 \
  --xueqiu-binding-manifest /absolute/owner-only-xueqiu-binding.json
```

同时启用两组能力时，把 `--xueqiu-binding-manifest` 加到第一条 `serve` 命令。
这个清单文件只保存定位信息，不保存 Cookie、BWS 令牌或密钥内容。服务会在原终端
列出即将开放的具体权限，要求输入 `enable`，随后在 `Connector>` 中执行 `unlock`。

在 VPS 上初始化，并为所需权限配对：

```bash
export HERMES_REACH_CONNECTOR_HOST="<trusted-device-private-ip>"

uv run hermes reach connector init \
  --role vps \
  --state-directory /absolute/vps-state

uv run hermes reach connector pair \
  --state-directory /absolute/vps-state \
  --connector "wss://${HERMES_REACH_CONNECTOR_HOST}:8765" \
  --device-label hermes-vps \
  --scope reddit:read.post:public \
  --scope instagram:browse.explore:account_visible
```

在可信设备上执行 `pending`，核对两端信息后再执行 `approve <pairing-id>`。VPS
启动 Hermes 时指向配对状态：

```bash
HERMES_REACH_VPS_STATE_DIRECTORY=/absolute/vps-state uv run hermes \
  --skills reach:agent-reach \
  --toolsets reach
```

Connector 的网络配置、授权与撤销，以及 Bitwarden、审计和故障处理方式，见
[Connector 安全与运维](docs/connector-security.md)。

## 安全边界

- 所有操作都是只读的，来源和操作必须明确指定。
- 请求有超时、结果数量、响应字节和分页限制。
- 没有通过审核的后端时，调用直接失败，不会改用其他实现。
- 平台账号会话和凭据留在可信设备；VPS 仍能看到它发出的查询和收到的结果。
- VPS 失守会暴露设备身份和本地 Connector 状态，攻击者也可能消耗未过期的剩余
  授权。撤销后，后续请求会被拒绝。

Web `read.url`、GitHub 和 LinkedIn 暂未开放。原因和复审条件记录在
[Agent-Reach 复用边界](docs/agent-reach-reuse-boundary.md) 和
[后端决策目录](docs/agent-reach-decisions/)。

## Agent-Reach 边界

Hermes Reach 本身不实现各平台的访问逻辑。现有 33 项操作都由固定在指定提交的
Agent-Reach fork 执行。Hermes Reach 负责输入校验、授权、隔离调用、结果限制、脱敏、
回执和审计。

[插件边界](docs/agent-reach-plugin-boundary.md) 记录了双方职责。
[发布指南](docs/releasing.md) 包含版本发布、文件验收和回滚步骤。

## 停用与卸载

源码环境：

```bash
uv run hermes plugins disable reach
uv pip uninstall --python .venv/bin/python hermes-reach
```

从 wheel 安装的环境：

```bash
"$HERMES_BIN" plugins disable reach
uv pip uninstall --python "$HERMES_PYTHON" hermes-reach
uv pip check --python "$HERMES_PYTHON"
```

先停用，再启动一个不包含 Reach 的新 Hermes 进程，最后卸载软件包。Hermes 的
`plugins remove`、`plugins rm` 和 `plugins uninstall` 只管理目录插件，
不能替代 Python 包管理器。

## 开发

```bash
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

Hermes Reach `0.1.0a2` 使用 [MIT License](LICENSE)。
