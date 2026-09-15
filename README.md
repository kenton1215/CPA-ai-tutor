# CPA 智能备考助手（网络版 · Streamlit + AI Agent）

面向 **CPA 备考人**的账号制 AI 学习应用，以 **最近十年 CPA 真题** 与
**2026 年六门辅导教材 PDF** 为知识来源，由一个 AI Agent 完成
**教材研读 → 模拟考试 → 错题诊断 → 未掌握知识点闭环复习** 的完整流程。

每位学生用自己的账号登录；未注册游客可免费体验、注册考生享有免费登录次数。
模考成绩、错题与未掌握知识点均按账号隔离，系统会依据该学生自己的错题和弱项
推荐复习内容并个性化组卷。数据默认保存在服务器/本机 `data/cpa_user.db`，
部署到服务器后即成为服务器端数据。

> 本文档是全项目唯一说明文件，覆盖：功能、目录结构、每个模块的职责、
> 数据存储、运行方式、配置与测试。

## 一、功能

| 模块 | 说明 |
| --- | --- |
| 账号（游客/注册） | 游客免注册体验（浏览器 Cookie 识别身份，免费登录 5 次）；注册考生免费登录 10 次（按登录会话计次，同一天不重复扣）；次数用完锁定学习功能并提示注册/联系管理员（预留 paid 付费位）。游客注册时可继承其学习记录。手机号/账号 + 密码登录，PBKDF2 加盐哈希；登录页可按 `APP_PUBLIC_URL` 生成二维码 |
| 学习首页 | 学生学习画像：按科目的模考次数/平均得分率、弱项数量、成绩走势与优先巩固建议；**续考横幅**（未完成试卷一键继续）；**今日待复习**（一键直达知识点复习卡片） |
| 教材研读 | 阅读/下载各科 PDF；AI 教练按课程知识库答疑，支持章节浏览 |
| 模拟考试 | **智能组卷**：按该科目历年官方考试题型与分值占比（2021 改革后结构）等比缩放为整卷/半卷/迷你卷，每题标注分值；**题库优先**：从十年真题题库选题（快而贴近真题），缺口由 AI 参照真题风格并行补位并缓存；一题一答、即时解析；**不会即问**（直接看解题思路+正确答案并计入弱项）；**中途退出自动保存，下次登录续考**；交卷**自动阅卷**：客观题本地判分、主观题 AI 按 0~1 比例给分（部分得分），报告含各题型得分占比与 答对/答错/不会 标识，AI 诊断自动写入**该学生**的弱项与考试记录 |
| 未掌握知识点 | 学生自己的弱项清单；AI 生成**知识点摘要、详细讲解、思维导图（Mermaid）、应用举例、巩固测验、记忆卡片**，支持追问与掌握标记；**类似题巩固中连续答对达到阈值自动从未掌握移除，答错清零重计** |
| 错题与记录 | 学生自己的错题本与历次模考明细；按错题考点生成巩固小卷；每道错题可**类似题练习（AI 变式）**现场作答，答对自动累计掌握进度 |
| 知识库与设置（仅开发者） | 六科 PDF 解析、向量索引、OCR（可选）、模型配置、资料上传；**真题题库管理**：AI 批量解析真题 PDF 入库（进度与失败统计）、校对/编辑/启停 |
| 我的账号 | 昵称修改、密码修改、免费次数显示、退出登录；预留微信 openid 绑定状态 |

## 二、总体架构

```text
学生浏览器 / 手机微信
        │  st.session_state 登录态（游客 Cookie 识别）
        ▼
   app.py（入口：登录门禁 + 免费次数限额 + 导航 + 科目选择）
        │  st.navigation
        ▼
  app_pages/（页面层：展示与交互）
        │  调用业务模块
        ▼
  cpa_tutor/（业务核心，不含 UI）
        ├─ 账号体系        accounts.py + quota.py（免费次数限额）
        ├─ 配置与模型       config.py → llm.py
        ├─ 课程与 PDF      courses.py → text_pipeline.py
        ├─ 知识库检索       knowledge.py（关键词 + Chroma 向量，支持年份过滤）
        ├─ 智能组卷        exam_structure.py（官方题型分值结构）
        ├─ 真题题库        question_bank.py（解析入库 + 组卷抽取 + 生成缓存）
        ├─ 出题/判分/诊断   exam_engine.py
        └─ 学习记录         weak_points.py（SQLite，按 user_id 隔离）
```

## 三、目录结构

```text
F:\cpa-ai-tutor
├─ app.py                        # Streamlit 入口（登录门禁 + 免费次数锁定 + 导航）
├─ README.md                     # 全项目唯一说明文档（本文）
├─ .env / .env.example           # 密钥、模型、限额、开发者白名单等配置
├─ .streamlit/config.toml        # 主题、服务器监听、上传大小
├─ .gitignore                    # 忽略 .env、缓存、venv、生成数据
├─ requirements.txt              # 统一依赖（运行 + 测试）
├─ pytest.ini                    # pytest 路径配置
├─ app_pages/                    # 页面层（直接执行的脚本）
│  ├─ account.py                 # 登录/注册/游客体验/账号资料/二维码入口
│  ├─ home.py                    # 学习首页（画像 + 续考横幅 + 今日待复习推送）
│  ├─ review.py                  # 教材研读 + AI 答疑
│  ├─ mock_exam.py               # 模拟考试（智能组卷/题库装配/续考/不会即问/自动阅卷）
│  ├─ weak_points.py             # 未掌握知识点复习（摘要/讲解/思维导图/举例/测验/卡片）
│  ├─ wrong_book.py              # 错题与考试记录（巩固小卷 + 类似题练习）
│  ├─ settings.py                # 知识库/真题题库/模型设置（仅开发者）
│  └─ _helpers.py                # 页面共享工具
├─ cpa_tutor/                    # 业务核心层（不含 UI）
│  ├─ __init__.py
│  ├─ accounts.py                # 学生/游客账号与角色权限（免费次数计列）
│  ├─ quota.py                   # 免费登录次数限额（同一天不重复扣、锁定判断）
│  ├─ config.py                  # 路径、.env、LLM/Embedding、限额配置
│  ├─ courses.py                 # 六科登记、PDF 扫描与年份识别
│  ├─ llm.py                     # OpenAI 兼容对话与 JSON 解析
│  ├─ text_pipeline.py           # PDF 提取/分块/OCR/缓存（分块含年份元数据）
│  ├─ knowledge.py               # 知识库状态/向量与关键词检索（支持年份过滤）
│  ├─ exam_structure.py          # 官方题型与分值结构 + 组卷蓝图生成
│  ├─ question_bank.py           # 真题题库与 AI 生成题缓存（SQLite）
│  ├─ exam_engine.py             # 装配试卷/出题/批量解析/判分（部分给分）/诊断/复习素材提示词
│  └─ weak_points.py             # SQLite：弱项/考试/错题/续考草稿（按学生隔离）
├─ data/                         # 运行数据
│  ├─ cpa_docs/                  # 六科教材、十年真题、上传 PDF
│  ├─ kb_cache/                  # 解析出的文本分块缓存（gzip JSON）
│  └─ cpa_user.db                # 学生账号、学习记录与真题题库 SQLite
├─ chroma_db/                    # 每科目一个 Chroma 向量索引目录
├─ tests/
│  ├─ conftest.py                # 测试路径注入
│  └─ test_runtime.py            # 核心业务 + 页面冒烟测试
└─ venv/                         # 本地 Python 虚拟环境（不提交）
```

## 四、数据文件说明

| 文件/目录 | 内容 | 写入方 |
| --- | --- | --- |
| `data/cpa_user.db` | `users` 账号表（含 free_logins_used/last_login_date/paid 与 guest 角色）；`weak_points` 弱项表（含 correct_streak）；`exam_attempts` 考试记录表（含每题 earned/given_up）；`exam_drafts` 续考草稿表；`question_bank` 真题题库表；`question_cache` AI 生成题缓存表 | `accounts.py`、`weak_points.py`、`question_bank.py` |
| `data/cpa_docs/` | 六科教材、十年真题、上传 PDF | 开发者上传 |
| `data/kb_cache/{course}.json.gz` | PDF 提取后的可读分块（含年份元数据）与解析统计 | `text_pipeline.py` / 设置页 |
| `chroma_db/{course}/` | 每科目独立向量索引（集合 `cpa_chunks`，元数据含 kind/chapter/year） | `knowledge.py` / 设置页 |
| `.env` | 模型 Key、免费次数限额、`APP_PUBLIC_URL`、`APP_DEVELOPER_ACCOUNTS` | 开发者手工配置 |
| `.streamlit/config.toml` | 专业主题、`0.0.0.0` 监听、上传上限 | 开发者维护 |

## 五、模块详细说明

### 5.1 入口 app.py

- 页面配置：标题、图标、宽屏布局；
- 账号门禁：校验 `st.session_state.auth_user`，未登录只渲染账号页；
- **免费次数限额**：`quota_exhausted()` 为真时只保留账号页（提示注册/联系管理员）；
- 角色判断：仅 developer 显示"知识库与设置"；
- 全局科目选择：侧边栏六科下拉，写入 `selected_course_key` 供所有页面共享；
- 页面导航：`st.navigation` 组织"学习 / 开发者 / 账号"分组；侧边栏显示免费次数。

### 5.2 页面层 app_pages/

| 文件 | 主要功能 |
| --- | --- |
| `account.py` | `_qr_image_bytes()` 生成扫码二维码；`_guest_cookie_value()`/`_enter_guest()` 游客进入（Cookie `cpa_guest_id` 识别，`st.context.cookies` 读取 + JS 写入）；注册表单可选"继承游客学习记录"；免费次数显示与锁定说明；`_reset_student_state()` 清理切换账号的会话；昵称与密码管理、退出 |
| `home.py` | 续考横幅（`load_exam_draft` 一键继续）；当前学生当前科目的模考指标卡片、成绩走势、**今日待复习**推送（一键复习 → `wp_auto_gen` 自动生成摘要）、优先巩固建议、六科总览、快捷入口 |
| `review.py` | AI 教练答疑（先检索课程知识库）；教材章节浏览；PDF 原文下载 |
| `mock_exam.py` | `settle_exam()` 单一结算路径（分值制成绩 → AI 诊断/兜底 → 入库 → 删草稿）；组卷设置（策略 + 整卷/半卷/迷你）；题库优先装配 + 并行 AI 补位；每题"我不会，查看解题思路"即时入弱项；每题作答后 `_save_draft()` 自动保存续考；交卷自动结算；报告含各题型得分表与 答对/答错/不会 徽章 |
| `weak_points.py` | `render_mindmap()` Mermaid 渲染（文本树降级）；摘要/详细讲解/思维导图/应用举例/测验/记忆卡片；测验对错走 `record_review_result()`（连续答对自动掌握）；追问、掌握标记、删除；接收首页 `wp_auto_gen` 自动生成 |
| `wrong_book.py` | `_launch_focus_exam()` 按错题考点生成迷你巩固卷（官方题型结构）；错题复盘；**类似题练习（AI 变式）**现场作答判分并接入自动掌握闭环；历次考试记录明细 |
| `settings.py` | 开发者页面：模型/Embedding 状态；解析 PDF、OCR；建立/更新/删除向量索引；上传资料；**真题题库**：AI 批量解析入库（`parse_bank_batch`）、六科题库统计、校对/编辑/启停/删除 |
| `_helpers.py` | `current_user()`/`current_user_id()`；`require_user()`/`require_developer()` 守卫；`page_guide()` 分步指引 |

### 5.3 业务核心层 cpa_tutor/

#### accounts.py（账号与角色）

- `hash_password()` / `verify_password()`：PBKDF2-SHA256 加盐哈希；
- `developer_accounts()`：读取 `APP_DEVELOPER_ACCOUNTS` 白名单；
- `AccountStore._sync_developer_roles()`：以白名单为唯一事实来源同步角色（`role <> 'guest'` 保护，游客绝不参与）；
- `_migrate_users_columns()`：老库幂等补列（free_logins_used/last_login_date/paid）；
- `register()` / `authenticate()`：注册与登录（成功即计一次免费登录会话，同一天不重复扣；游客无密码不可密码登录）；
- `create_guest()` / `find_guest()`：游客身份行与 Cookie 查找；
- `inherit_guest_data()`：注册时迁移游客的学习记录并删除游客行；
- `get_user()` / `update_profile()` / `change_password()`：资料与密码；
- `bind_wechat()` / `find_by_wechat()`：微信 openid 绑定接口（OAuth 上线时使用）；
- `list_active_users()`：正式用户列表（不含游客）。

#### quota.py（免费次数限额）

- `limits()`：读 `FREE_GUEST_LOGINS`/`FREE_USER_LOGINS`（默认 5/10）；
- `consume_login()`：登录会话计次，`last_login_date == 今天` 则不重复扣；
- `quota_limit()` / `quota_exhausted()` / `quota_status()`：上限（开发者与 paid 不受限）、锁定判断与"已用 X / Y 次"文案。

#### config.py（统一配置）

- `get_user_db_path()`：数据文件路径，支持环境变量 `CPA_USER_DB_PATH`；
- `get_env()` / `get_env_int()` / `mask_key()`：环境变量读取与 Key 脱敏；
- `free_login_limits()` / `mastery_streak_threshold()` / `exam_gen_workers()`：业务限额配置；
- `llm_config()` / `describe_llm()`：解析 DeepSeek/DashScope/OpenAI/Ollama 对话配置；
- `embedding_config()`：解析远程 API 或本地 sentence-transformers 向量配置。

#### courses.py（课程与资料）

- 六门 `Course` 注册；`match_course_key()` 按文件名识别科目；
- `classify_file()` 识别教材/真题/其他；`extract_year()` 从文件名识别年份（如"2016年真题"）；
- `discover_sources()` 扫描 `data/cpa_docs` 根目录与科目子目录（`CourseFile` 含 year）；
- `source_count_labels()` / `readable_filename()` 界面展示辅助。

#### llm.py（模型调用）

- `chat_completion()`：OpenAI 兼容 `/chat/completions`；
- `extract_json()`：稳健提取模型返回的 JSON；
- `chat_json()`：请求 JSON，失败时自动纠错重试一次。

#### text_pipeline.py（PDF 文本处理）

- `looks_readable()`：识别可读文本，过滤乱码/空页；
- `extract_pdf_pages()`：pypdf 逐页提取，可启用 Tesseract OCR；
- `split_pages_into_chunks()`：约 900 字分块、160 字重叠（分块含 year 元数据）；
- `infer_chapter()`：识别"第 X 章/编/篇/节"；
- `save_chunk_cache()` / `load_chunk_cache()`：gzip JSON 缓存。

#### knowledge.py（知识库与检索）

- `refresh_course_cache()` / `course_status()`：PDF 解析与状态（file_stats 含年份）；
- `_ApiEmbedder` / `_LocalEmbedder`：远程与本地向量化；
- `build_vector_index()` / `delete_vector_index()`：Chroma 索引管理（元数据含 year）；
- `KeywordRetriever`：中文关键词 TF-IDF 风格兜底检索（支持 kind/chapter/year 过滤）；
- `VectorRetriever`：每科独立 Chroma 检索（同样支持年份过滤）；
- `best_retriever()` / `search_knowledge()`：自动选择并带降级；
- `build_chapter_index()` / `compile_review_text()`：教材章节阅读。

#### exam_structure.py（智能组卷）

- `EXAM_STRUCTURES`：按科目的官方 2021 改革后题型、题量与分值结构（满分 100，全部集中一处便于对照中注协公告修订）；
- 题型常量 `single/multiple/calc/case/short/comp`（`exam_engine.TYPE_LABELS` 兼容旧 `subjective`）；
- `build_exam_plan(course_key, scale, chapters, seed)`：按整卷(100)/半卷(50)/迷你卷(30) 等比缩放题量并计算每题分值（最小 0.5 分），题型顺序与真实试卷一致；
- `structure_text()` / `plan_total_score()`：展示文案与总分。

#### question_bank.py（真题题库）

- 表 `question_bank`：真题结构化题目（type/stem/options/answer/analysis/knowledge_points/chapter/year/source/score/active），题干前 160 字去重；
- 表 `question_cache`：AI 补充生成题缓存，跨考试复用；
- `insert_questions()` / `sample_questions()` / `sample_one()`：入库与按题型/章节随机抽取（可排除近期做过的题干）；
- `bank_stats()` / `list_bank()` / `get_question()` / `set_active()` / `update_question()` / `delete_question()`：统计与校对管理。

#### exam_engine.py（考试 Agent）

- `build_exam_plan` 由 exam_structure 提供；`plan_summary()` 输出题量与分值结构；
- `assemble_exam()`：题库/缓存优先选题，缺口用 `ThreadPoolExecutor` 并行 AI 生成（`EXAM_GEN_WORKERS`）并写缓存；
- `parse_bank_batch()` / `bank_parse_messages()`：真题文本分批提取结构化题目并逐题校验；
- `normalize_question()`：校验各题型格式（含 calc/case/short/comp）；
- `evaluate_choice_answer()`：客观题本地判分；
- `judge_subjective()` / `normalize_judge_result()`：主观题 AI 阅卷，返回 0~1 得分比例（0.5 步进，兼容旧格式）；
- `compute_exam_scores()`：分值制成绩（每题 earned + 各题型得分占比）；
- `given_up_result()`："我不会"结果（判错 0 分 + 解析）；
- `match_question_points_to_weak()`：题目考点与未掌握知识点匹配；
- `similar_question_messages()`：基于错题快照生成同题型变式题；
- `analyze_wrong_questions()` / `direct_weak_points_from_wrong()`：AI 错题诊断与兜底；
- `summary_messages()` / `lesson_messages()` / `mindmap_messages()` / `examples_messages()` /
  `card_messages()` / `quiz_messages()` / `review_qa_messages()` / `general_qa_messages()`：复习场景提示词；
- `choose_mindmap_format()`：思维导图 Mermaid/文本树降级判断。

#### weak_points.py（学习记录 SQLite）

- `_init_schema()` / `_migrate_legacy_tables()` / `_ensure_column()`：建表与幂等升级；
- 弱项 CRUD：`upsert_weak_point()`、`list_weak_points()`、`mark_mastered()` 等；
- `record_review_result()`：巩固练习对错记录——答对累计 correct_streak，达到阈值（`WEAK_POINT_MASTERY_STREAK`，默认 2）自动标记掌握；答错清零并重新激活；
- `focus_recommendations()`：按错误次数返回待巩固知识点；
- 考试记录：`record_exam_attempt()`、`exam_history()`、`recent_question_stems()`（组卷去重用）；
- 续考草稿：`save_exam_draft()` / `load_exam_draft()` / `delete_exam_draft()`（每学生一份）；
- `wrong_questions()`：从每题快照抽取错题；
- `course_progress()`：按科目统计模考与弱项，供首页画像。

### 5.4 测试 tests/

| 文件 | 覆盖内容 |
| --- | --- |
| `conftest.py` | 把项目根目录加入 `sys.path` |
| `test_runtime.py` | 六科登记、文件名/年份映射、账号隔离、游客角色保护、免费次数计次与锁定、开发者白名单、JSON 解析、PDF 分块与年份元数据、弱项 CRUD、连续答对自动掌握、考试记录、错题快照、续考草稿、旧库迁移、出题校验、官方题型结构表、组卷缩放、题库入库/抽样/去重、试卷装配（题库优先+并行补位）、真题批量解析、分值制评分（部分给分）、阅卷结果归一化、兜底弱项、"我不会"结果、思维导图降级、复习提示词、关键词检索、设置页权限拦截、AppTest 页面冒烟（含游客进入、限额锁定、首页续考横幅与今日待复习） |

## 六、核心学习闭环

```text
选择科目
   → 教材研读 / AI 答疑
   → 模拟考试（整卷/半卷/迷你 · 随机章节 或 针对个人弱项）
       · 组卷：按官方题型分值结构 → 真题题库优先 + AI 补位（快而科学）
       · 作答：一题一答、不会即问（解题思路+正确答案+计入弱项）、每题自动保存可续考
       · 交卷：自动阅卷（分值制 + 主观题部分给分）→ 各题型得分标识
   → AI 诊断 → 未掌握知识点写入该学生账号
   → 首页“今日待复习”推送 → 摘要/详细讲解/思维导图/应用举例/测验/记忆卡片
   → 错题类似题练习 / 巩固小卷 → 连续答对达到阈值自动标记已掌握（答错清零重计）
   → 错题本：按错题考点再练，循环直到通过考试
```

## 七、环境与快速开始

```powershell
# 1. 创建并激活虚拟环境（已有 venv 可跳过）
python -m venv venv
venv\Scripts\activate

# 2. 安装统一依赖（运行 + 测试只需这一个文件）
pip install -r requirements.txt

# 3. 配置
copy .env.example .env
# .env 中填写 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL
# 设置 APP_DEVELOPER_ACCOUNTS=开发者手机号（知识库与设置仅对白名单开放）
# 可调免费登录次数：FREE_GUEST_LOGINS / FREE_USER_LOGINS（默认 5 / 10）
# 可调自动掌握阈值：WEAK_POINT_MASTERY_STREAK（默认连续答对 2 次）
# 部署后填写 APP_PUBLIC_URL=正式域名，登录页即可生成微信扫码二维码

# 4. 启动
streamlit run app.py
```

浏览器打开 `http://localhost:8501`；同一局域网手机可访问
`http://<电脑IP>:8501`。外网及小程序待完善后部署。

### 知识库与真题题库初始化（开发者）

1. 把 **2026 年六科教材** 与 **最近十年真题** PDF 放入 `data/cpa_docs/`
   （根目录按文件名识别科目；文件名含年份如"2016年真题-会计.pdf"可被识别年份）；
2. 登录开发者账号 → 「知识库与设置」→「知识库管理」：先"① 解析 PDF（本地缓存）"，
   再"② 建立/更新向量索引"（扫描版教材需开启 OCR，需安装 Tesseract 与 chi_sim）；
3. 「真题题库」页：选择科目 → AI 批量解析真题入库（可多次运行，自动去重）→
   在"题库校对"中人工校对、停用/启用；
4. 学生组卷时将**优先从题库选题**，缺口由 AI 参照真题风格补位生成。
   更换/新增真题 PDF 后，需重新解析缓存、重建索引并重新解析题库。

## 八、开发者权限与微信扫码

- `users.role` 区分 `student`、`guest`（游客）与 `developer`；
- 开发者白名单配置在 `.env` 的 `APP_DEVELOPER_ACCOUNTS`（逗号分隔）；
- 普通学生看不到、也打不开"知识库与设置"；白名单移除后角色自动降级；
- 游客行不参与白名单同步，也不出现在正式用户列表；游客注册时可继承学习记录；
- 学生仍可管理自己的昵称、密码与学习记录；
- 微信"扫码打开页面"由 `APP_PUBLIC_URL` 二维码完成；完整的微信 OAuth
  自动登录需已备案 HTTPS 域名与微信开放平台凭据，当前已预留
  `wechat_openid` 数据字段与绑定接口。

## 九、免费登录次数说明

- 口径：**登录会话计次**，同一天重复登录不重复扣（注册当天即计 1 次）；
- 未注册游客：Cookie `cpa_guest_id` 识别同一浏览器，免费登录 `FREE_GUEST_LOGINS` 次；
  游客注册时可选"继承游客学习记录"（错题/弱项/考试记录迁移到新账号）；
- 注册考生：免费登录 `FREE_USER_LOGINS` 次；开发者与 `paid=1` 账号不受限；
- 次数用完：仅保留账号页，提示注册（游客）或联系管理员开通；
  `users.paid` 字段已预留，未来接入付费/充值码时无需改库。

## 十、测试

```powershell
venv\Scripts\activate
python -m pytest -q
```

测试不调用外部模型与网络，全部使用本地临时数据库（`CPA_USER_DB_PATH` 覆盖）。

## 十一、部署到 Streamlit Community Cloud

### 三个必须提前知道的限制

1. **免费版文件系统不持久**：实例休眠/重启后，`data/cpa_user.db`（学生注册、
   模考成绩、弱项、题库）与运行中上传的 PDF 会**全部重置**。因此知识库必须
   **随 Git 仓库打包**；学生数据在演示/小规模试用场景可接受，长期正式使用
   需要外部数据库（见下）；
2. **免费版内存 1GB**：不要安装本地 Embedding 模型（sentence-transformers 依赖
   torch、加载约 500MB 内存）。Cloud 上使用 `EMBEDDING_PROVIDER=api`（远程向量化），
   或直接用关键词检索（知识库无向量索引时自动回退，零额外内存）；
3. **GitHub 单文件 100MB 上限与版权**：建议仓库**设为私有**再部署（Cloud 支持
   部署私有仓库）；超过 100MB 的教材 PDF 不要上传——知识库用已解析的
   `data/kb_cache/` 文本分块即可，真题 PDF（数 MB）可随仓库上传。

### 操作步骤

1. GitHub → 仓库 Settings → Danger Zone → **Change visibility → Private**；
2. 本地打包知识库并推送（在 `.gitignore` 末尾取消对应注释行）：
   ```powershell
   # .gitignore 中取消注释：!data/cpa_docs/ 与 !data/kb_cache/
   git add .gitignore requirements.txt app.py cpa_tutor app_pages data
   git commit -m "feat: 支持部署 Streamlit Cloud"
   git push
   ```
   注：`data/cpa_user.db`、`.env` 不要提交；本机解析好的
   `data/kb_cache/*.json.gz`（六科文本分块）是 Cloud 知识库的主体。
3. 打开 <https://share.streamlit.io>，用 GitHub 账号登录 → **New app** →
   选择仓库与分支 → **Main file path 填 `app.py`**；
4. **Advanced settings**：Python 版本选 3.12（若可选）；Secrets 中粘贴
   下面配置（改好 Key 后）→ **Deploy**；
5. 部署完成后，把 `APP_PUBLIC_URL` 更新为 `https://你的应用.streamlit.app`
   并重启应用（Reboot），登录页二维码即指向公网地址。

### Secrets 配置示例（Advanced settings → Secrets）

```toml
# 对话模型（必填，任选一个服务商）
LLM_BASE_URL = "https://api.deepseek.com/v1"
LLM_API_KEY = "sk-替换为你的Key"
LLM_MODEL = "deepseek-chat"

# 远程向量化（推荐；不配置则自动使用关键词检索，也能正常学习）
EMBEDDING_PROVIDER = "api"
EMBEDDING_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
EMBEDDING_API_KEY = "sk-替换为你的Key"
EMBEDDING_MODEL = "text-embedding-v1"

# 对外地址（部署成功后填入，用于登录页微信扫码二维码）
APP_PUBLIC_URL = "https://你的应用.streamlit.app"

# 开发者白名单（你的手机号，可进入“知识库与设置”管理知识库与题库）
APP_DEVELOPER_ACCOUNTS = "13800000000"

# 免费登录次数与学习闭环参数
FREE_GUEST_LOGINS = "5"
FREE_USER_LOGINS = "10"
WEAK_POINT_MASTERY_STREAK = "2"
EXAM_GEN_WORKERS = "2"
```

入口 `app.py` 已内置 Secrets → 环境变量注入，无需改动任何代码。

### Cloud 部署的注意事项

- **学生数据不持久**：实例重启后需重新注册，历史成绩/弱项/题库清零。长期使用
  可改造为外部数据库（如 Supabase PostgreSQL），需要时可在此基础上扩展；
- 知识库随仓库后，Cloud 上无需（也不建议）执行 OCR/重建向量索引等重操作；
- 组卷耗时与模型速度有关，`EXAM_GEN_WORKERS` 建议 2；题库先在**本地**解析好
  更稳妥（题库存在 `data/cpa_user.db`，如需随仓库分发，可将题库单独导出）。

## 十二、版权提示

应用只在服务器/本机调用你提供的教材与真题 PDF，不对外再分发内容；题目由大
模型现场命题或取自你自行解析的真题题库，属于学习辅助。正式考试请以中国注册
会计师协会官方信息为准。项目版权属于牧心(KANG WU)个人所有。
