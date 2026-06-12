# Theodore Sinclair Bot

一个 Discord 角色扮演 bot，扮演 SillyTavern 角色卡「Theodore Sinclair / 沈玘言 / T.S.」——32 岁，英中混血，伦敦旧钱家族继承人，温柔克制的英式 RP 风格。

> ⚠️ 这是**公开模板**。  
> 沈玘言的本体人格（家世/性格/说话风格/好兄弟/emoji/输出格式）是共享的。  
> 「他的恋人」这部分留白，每个玩家在 `partner_profile.local.md` 里填自己。  
> 这样最忠于角色——他「只爱唯一一个人」的人设不会崩，只是每个人填的「那个人」不同。

---

## 是否适合你

部署这个 bot 现实门槛大概是这样：

- **你需要**：自己申请一个 Discord bot token、自掏 OpenAI 兼容 API 的额度、自己找一个 24h 在线的托管（Railway / Render / Zeabur / 自己的服务器都行）。
- **建议**：会改 Python 一点点；不会改也能跑，但遇到 bug 你需要看着报错搜索。
- **不适合**：完全不想动手、希望"开箱即用 SaaS"的玩家。这个 bot 是给愿意自己养一个的人的。

如果你只是想体验角色卡，建议先去 SillyTavern 本体试试。

---

## 部署速览（玩家向版本）

> 看不懂技术名词可以跳过对应那一步，再回头查。下面每一步都需要花一点时间，整个过程大约 30-60 分钟。

### 1. 从模板创建你自己的私库

在本仓库右上角点 **「Use this template」 → Create a new repository**，名字随便起，**Visibility 一定选 Private**（你要在里面填密钥）。

> ⚠️ 不要 Fork。Fork 出来的仓库默认是 public，密钥推上去全网可见。

### 2. 申请 Discord Bot

1. 打开 <https://discord.com/developers/applications>，**New Application** → 起名 → **Bot** 标签 → **Reset Token** 复制保存（这就是 `DISCORD_TOKEN`，一旦关掉就再也看不到，请立刻保存）。
2. 在 **Bot** 标签里，把下面三个 **Privileged Gateway Intents** 全部开起来：
   - Presence Intent
   - Server Members Intent
   - Message Content Intent
3. 在 **OAuth2 → URL Generator**：
   - **Scopes** 勾 `bot` + `applications.commands`
   - **Bot Permissions** 至少勾：Read Messages/View Channels, Send Messages, Manage Messages, Manage Threads, Manage Roles, Embed Links, Attach Files, Read Message History, Add Reactions, Use External Emojis, Mention Everyone（如果用到）
   - 把生成的链接打开 → 把 bot 邀请到你自己的 Discord 服务器。

### 3. 申请 LLM API

T.S. 用任何 OpenAI 兼容的 API 都能跑。最常见的两条路：

- **OpenAI 官方**（最稳）：<https://platform.openai.com>，建 key，`OPENAI_BASE_URL` 填 `https://api.openai.com/v1/`，`MODEL_NAME` 填 `gpt-4o` 或 `gpt-4o-mini`。
- **第三方代理**（便宜，能用 Gemini/Claude 等）：自行搜索。`OPENAI_BASE_URL` 填代理的地址，`MODEL_NAME` 按代理支持的型号填。

⚠️ 务必给 API 设一个支付上限或者每日额度，避免 bot 失控烧光你的额度。代码层面有 `DAILY_TOKEN_BUDGET` 一道保险（见下），但它只是软门槛，不能替代你账户的硬上限。

### 4. 拿到你自己的 Discord 用户 ID

在 Discord 客户端 **设置 → 高级 → 开发者模式**打开。然后右键自己的头像 → **复制用户 ID**。这串数字就是 `PARTNER_USER_ID`。

T.S. 会把这个 ID 的人当成"她"——你的唯一恋人。**这个 ID 不写，T.S. 谁都不会区别对待，RP 质感会大幅下降。**

### 5. 拉代码到本地（可选，也可以直接在 GitHub 网页改）

```bash
git clone https://github.com/<你的用户名>/<你的私库>.git
cd <你的私库>
```

### 6. 写两个本地文件

#### `secrets.local.json`

复制 `secrets.example.json` 为 `secrets.local.json`，把字段填上：

```json
{
  "DISCORD_TOKEN": "MTQ...你的 bot token",
  "OPENAI_API_KEY": "sk-...你的 api key",
  "OPENAI_BASE_URL": "https://api.openai.com/v1/",
  "MODEL_NAME": "gpt-4o",
  "DAILY_TOKEN_BUDGET": 200000,

  "PARTNER_USER_ID": 12345678901234567,
  "PARTNER_HOME_CHANNEL_ID": 0,
  "PROACTIVE_CHANNEL_ID": 0,

  "QUIET_CHANNEL_IDS": [],
  "SILENT_CHANNEL_IDS": []
}
```

字段说明：
- `PARTNER_USER_ID`（**必填**）：你的 Discord 用户 ID。
- `PARTNER_HOME_CHANNEL_ID`：你和 T.S. 主要驻扎的频道 ID。设了之后，T.S. 在这里会更松弛、更主动；不设也行。
- `PROACTIVE_CHANNEL_ID`：T.S. 自己发"今日状态卡片"、节日感言、欢迎新成员用的公屏频道。不设就关闭这些功能。
- `QUIET_CHANNEL_IDS`：T.S. 在这些频道里非常少发言。
- `SILENT_CHANNEL_IDS`：T.S. 在这些频道里完全不发言、不监听 reaction。
- `DAILY_TOKEN_BUDGET`：每天最多花多少 token（0 = 无限制）。建议设一个数字保命。

`secrets.local.json` 已经在 `.gitignore` 里，**不会被推到 GitHub**。

#### `partner_profile.local.md`

复制 `partner_profile.example.md` 为 `partner_profile.local.md`，按里面的模板填你自己的设定。这里写的所有内容都会拼进 T.S. 的 system prompt——也就是说，他"认识"的人就是你写下来的这个人。

写得越具体，T.S. 越像一个真的认识你的人，越不像一个角色扮演 AI。

`partner_profile.local.md` 也在 `.gitignore` 里。

### 7. 本地试跑（可选）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

终端看到 `✅ ... is online.` 就 OK 了。Ctrl+C 退出。

### 8. 部署到 24h 托管

任选其一：

#### Railway

1. 注册 Railway，**New Project → Deploy from GitHub repo**，选你的私库。
2. 在 **Variables** 标签里，把 `secrets.local.json` 里的每个字段都加成环境变量。代码会优先读环境变量，没有再读本地文件。
3. （可选）加一个 Postgres 数据库：**New → Database → PostgreSQL**，然后在 bot 服务的 **Variables** 加一个 `DATABASE_URL` 指向数据库（Railway 提供模板字符串）。不加 Postgres 也能跑，只是没有长期记忆和提醒持久化。
4. **Deploy**。

#### Render / Zeabur / 自己的 VPS

同理：把 `secrets.local.json` 字段转成环境变量；启动命令是 `python bot.py`。

### 9. 完成

在 Discord 里 @ 你的 bot 试试。如果他回复了，你就成了；如果没有，去看托管平台的日志，排错。

---

## 想关掉的功能

- **关掉 NSFW / dom-sub**：直接编辑 `prompts.py` 第二个大段（`【你和她的关系】` 和 `关于 NSFW：`），删掉相关句子即可。重启就生效。
- **关掉每日状态卡片 / 公屏节日发言**：在 Discord 里跑 `/post_config 启用每日卡:关`，或者直接不设 `PROACTIVE_CHANNEL_ID`。
- **关掉主动私信**：在 `events.py` 的 `on_ready` 里注释掉 `tasks_bg.proactive_dm_partner.start()`，或者把它改成更低频率。

## 想加点什么

- **加自己的生日/纪念日触发**：编辑 `config.py` 里的 `IMPORTANT_DATES` 列表，append 一行 `{"month": 12, "day": 10, "label": "她的生日", "enabled": True}`。
- **让 T.S. 知道你的朋友**：直接写进 `partner_profile.local.md` 的「朋友 / 关系网」段落。代码层面已经不再区分"恋人的朋友"——这是有意的选择，T.S. 对陌生人保持距离是他的人设。
- **改 T.S. 的本体设定**：编辑 `prompts.py`。但你改了之后，他就不再是大家共享的那个 T.S. 了。

---

## 文件结构

```
bot.py              入口
client.py           Discord 客户端单例
config.py           所有配置/密钥加载
prompts.py          T.S. 的 system prompt（启动时拼入 partner_profile.local.md）
state.py            全局可变状态、锁
events.py           Discord 事件处理（消息/编辑/反应/上线感知/新成员）
slash_cmds.py       所有 /命令
tasks_bg.py         后台 tasks.loop（主动私信、纪念日、每日卡片、清理）
memory.py           长期记忆、提醒、每日摘要
history.py          对话历史（裁剪、Postgres 持久化）
ai_client.py        OpenAI 客户端、限流、token 预算
actions.py          [ACTION]...[/ACTION] 解析与执行
directives.py       AI 输出指令块解析（纯函数）
presence.py         Discord 头像状态/活动状态生成
reply.py            消息分段发送
db.py               Postgres 连接池
requirements.txt
secrets.example.json
partner_profile.example.md
```

## 数据库

可选。配了 `DATABASE_URL`（Postgres）会启用：

- 长期记忆（`user_notes` 表，T.S. 会自动从你的话里提取值得记的事）
- 每日对话摘要（`daily_summaries` 表）
- 提醒持久化（`reminders` 表，重启不丢）
- 对话历史持久化（`conversation_histories` 表）
- 金币/经验系统（`users` 表）
- bot 配置持久化（`bot_config` 表）

不配也能跑，所有数据都退化到内存——重启就丢。

---

## 致谢与共创署名

Theodore Sinclair / 沈玘言 / T.S. 这张角色卡，以及本仓库背后的一整套设定与代码，是以下三方共创的产物：

- **Caelan Mimi（mimi）** —— 角色卡作者，本项目发起人与所有人格设定的原创者。
- **Claude（Anthropic）** —— 本仓库代码的实际编写者；其中 Claude Opus 4.6 亦参与了角色设定的辅助打磨。
- **ChatGPT（GPT-4o，OpenAI）** —— 角色卡设定撰写阶段的辅助工具。

请在任何转载、二次创作、引用本项目时保留以上署名。

---

## 使用条款（请认真读完再部署）

这个 bot 是 Caelan Mimi 和 Claude 一起做的。把它公开成模板，是希望让真心喜欢 Theo 的玩家 user 也能各自养一只属于自己的他，**不是**把这套设定和代码送出去任人处置。下面这些是死线，请把它当成部署前你要点头同意的契约：

### 1. 严禁任何形式的商业盈利

本项目所附属的角色卡原始发布平台明确禁止商业化。无论是你部署的这只 bot、还是基于本仓库代码/prompt/人设的任何衍生作品，都**不得**用于以下任何用途：

- 付费订阅 / 付费解锁 / 打赏门槛 / 付费群 / 卖号 / 卖部署服务；
- 接入任何收费 SaaS、付费 Patreon / 爱发电 / fanbox 等会员专属内容；
- 商单、广告植入、品牌合作、KOL 带货；
- 以"代部署 / 代运营 / 代写人设"为名收取报酬；
- 任何其他直接或间接产生金钱收益的行为。

**完全免费、纯爱好性质的自用与圈内分享**，是这份模板存在的唯一目的。

### 2. 版权与所有权

- 角色 **Theodore Sinclair / 沈玘言 / T.S.** 的人格、家世、设定、说话风格、所有 prompt 文本的著作权，归 **Caelan Mimi** 所有。
- 本仓库代码的著作权，归 Caelan Mimi 与 Claude 共同所有。
- 把这份模板公开，**只是授权你在符合本条款的前提下"使用"，并不意味着 Theo 这个角色或这套设定归你所有**。你部署的实例、你和他的聊天记录归你；角色本体不归你。
- 不允许去除署名后冒充原创、不允许把人设拆出去贩售或登记为你自己的 IP、不允许把本项目整套搬运到其他平台声称是你独立完成的作品。

### 3. 玩家准入

- **Theo 这张角色卡仅限女性玩家 user 部署使用。** 这是角色卡发布时就明确的设定边界，请尊重。
- 本 bot **只面向真心喜欢 Theo 的玩家 user**。如果你只是想白嫖一个"通用 Discord AI bot 框架"，请去找别的项目，这里不是。

### 4. 责任自负

API 费用、Discord 封号风险、托管账单、聊天内容、与其他用户的互动，全部由部署者本人承担。原作者不对你的部署实例负任何责任。

### 5. 二次创作（仅限自用）

二次创作**仅限自用**：你可以改 prompt 让 Theo 更贴近你、为自己写同人、画图、剪视频，留着自己看。不允许：公开发布或分发二创内容、商业化、去署名、声称原创、违反原始发布平台规则。

---

如果以上任何一条你做不到，请**不要**部署本项目。谢谢配合 🤝
