# Hermes Reach

中文 | [English](README_EN.md)

Hermes Reach 是 [Hermes Agent](https://github.com/NousResearch/hermes-agent)
的只读检索插件。插件向 Hermes 注册五个只读工具；平台调用、结果投影和 backend
选择由精确固定的 [Agent-Reach fork](https://github.com/izumi0uu/Agent-Reach)
负责。

当前源码版本是 `0.1.0a2`，状态为 Pre-Alpha。建议先在本地试用。

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

启动一个只加载 Reach 工具和路由 skill 的会话：

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

插件目录有 63 条只读 operation。当前 34 条已实现，其中 33 条有
Agent-Reach fork executor；YouTube comments 已实现请求契约但还没有 backend。

| 启用条件 | 来源 | Operation |
| --- | --- | --- |
| 默认本地可用 | RSS/Atom | `read.feed`、`browse.entries` |
| 默认本地可用 | Bilibili | `search.videos`、`read.video`、`browse.hot`、`browse.rank` |
| 默认本地可用 | YouTube | `search.videos`、`read.video`、`read.subtitles` |
| 默认本地可用 | V2EX | `browse.hot`、`browse.node_topics`、`read.topic`、`read.user` |
| 提供完整 Exa artifact | Exa | `search.web`、`search.code` |
| 配对并授权 Connector | Reddit | 7 条搜索、读取和浏览 operation |
| 配对并授权 Connector | Facebook | 4 条搜索、读取和浏览 operation |
| 配对并授权 Connector | Instagram | 4 条搜索、读取和浏览 operation |
| 配对并授权 Connector | Twitter/X | `search.posts` |
| 配对并授权 Connector | 小红书 | `search.notes` |
| 配对并授权 Connector | 雪球 | `search.stocks` |
| 当前不可用（未绑定） | YouTube | `read.comments` |
| 当前不可用 | Web、GitHub、LinkedIn、小宇宙及其余目录 operation | 没有通过审核的 executor |

每个环境的结果以 `reach status` 为准。安装某个命令、Cookie 或 API key
不会自动把对应来源变成 `available`。

## 五个工具

| 工具 | 用途 |
| --- | --- |
| `reach_status` | 查询来源、operation 和当前可用性 |
| `reach_search` | 搜索 1 到 5 个明确来源 |
| `reach_read` | 读取一个明确目标 |
| `reach_browse` | 浏览来源原生集合 |
| `reach_transcribe` | 转写受支持媒体；当前没有默认可用的转写 operation |

目录只包含读取操作。插件不提供发布、评论、点赞、关注或修改账号的能力。

## 安装公开预发布版

[GitHub Releases](https://github.com/izumi0uu/hermes-reach/releases) 是当前公开
发布通道。`v0.1.0a1` 不包含最终 33-operation 集成；如果发布页还没有
`v0.1.0a2`，请使用上面的源码安装。发布工作流上传 wheel、sdist 和
`SHA256SUMS`。GitHub 自动生成的 `Source code (zip)` 与
`Source code (tar.gz)` 不属于这三个受验收资产。

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
attestation subject；它只校验下载字节是否变化。

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

Exa 不使用 API key。Hermes 也不会从 PATH、npm 或编辑器配置中自动寻找
Node/mcporter。启动 Hermes 前需要一次性提供完整 artifact 声明：

```bash
export HERMES_REACH_EXA_NODE_EXECUTABLE=/absolute/path/to/node
export HERMES_REACH_EXA_NODE_SHA256="<64-lowercase-hex>"
export HERMES_REACH_EXA_MCPORTER_ROOT=/absolute/path/to/mcporter
export HERMES_REACH_EXA_MCPORTER_CLI=/absolute/path/to/mcporter/dist/cli.js
export HERMES_REACH_EXA_MCPORTER_TREE_SHA256="<64-lowercase-hex>"
export HERMES_REACH_EXA_CONFIG_PATH=/absolute/path/to/sterile-config.json
export HERMES_REACH_EXA_CONFIG_SHA256="<64-lowercase-hex>"
```

七项缺失、只提供一部分或摘要不匹配时，Exa 保持 `setup_required`。Web 和
Code 使用不同的固定方法，不能互相回退。配置细节见
[Exa backend 决策](docs/agent-reach-decisions/exa-mcporter-1.5.0.md)。

## 在可信设备运行 Connector

Reddit、Facebook、Instagram、Twitter、小红书和雪球默认不在 VPS 执行。
Connector 在你的电脑或其他可信设备上持有浏览器会话、Cookie 和 Bitwarden
访问能力。VPS 不保存平台凭据，但会保存自己的设备密钥、配对记录、能力快照、
grant 和回执账本。

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
该 manifest 只保存定位信息，不保存 Cookie、BWS token 或 secret 值。服务会在原终端
显示待启用的精确 scope，并要求输入 `enable`，随后在 `Connector>` 中执行 `unlock`。

在 VPS 上初始化并配对所需 scope：

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

可信设备执行 `pending`，核对两端信息后执行 `approve <pairing-id>`。VPS 启动
Hermes 时指向配对状态：

```bash
HERMES_REACH_VPS_STATE_DIRECTORY=/absolute/vps-state uv run hermes \
  --skills reach:agent-reach \
  --toolsets reach
```

完整的网络、授权、撤销、Bitwarden、审计和故障处理步骤见
[Connector 安全与运维](docs/connector-security.md)。

## 安全边界

- 所有 operation 都是只读的，来源和 operation 必须明确指定。
- 请求有超时、结果数量、响应字节和分页限制。
- 没有审核通过的 backend 会失败关闭，不会寻找宽松 fallback。
- 平台账号会话和凭据留在可信设备；VPS 仍能看到它发出的查询和收到的结果。
- VPS 失守会暴露设备身份和本地 Connector 状态，攻击者也可能消耗未过期的剩余
  grant。撤销会阻止后续请求。

Web `read.url`、GitHub 和 LinkedIn 当前被主动冻结。原因与重新审核条件记录在
[Agent-Reach 复用边界](docs/agent-reach-reuse-boundary.md) 和
[backend 决策目录](docs/agent-reach-decisions/)。

## Agent-Reach 边界

Hermes Reach 不复制平台 runtime。当前 33 个 executor 全部来自精确固定的
Agent-Reach fork；Hermes Reach 负责输入校验、授权、隔离调用、结果上限、脱敏、
回执和审计。

完整分工见 [插件边界](docs/agent-reach-plugin-boundary.md)。版本发布、artifact
验收和回滚步骤见 [发布指南](docs/releasing.md)。

## 停用与卸载

源码环境：

```bash
uv run hermes plugins disable reach
uv pip uninstall --python .venv/bin/python hermes-reach
```

wheel 环境：

```bash
"$HERMES_BIN" plugins disable reach
uv pip uninstall --python "$HERMES_PYTHON" hermes-reach
uv pip check --python "$HERMES_PYTHON"
```

先停用，再启动一个不包含 Reach 的新 Hermes 进程，最后卸载 wheel。Hermes 的
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
