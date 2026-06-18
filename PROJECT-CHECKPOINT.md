# AI内容成片工作台 2.0 项目检查点

## 当前目标
- 做一个可自用、未来可产品化销售的 AI 内容成片工作台。
- 核心流程：内容目标 -> 文案/口播 -> 分镜 -> 逐分镜素材匹配 -> 缺口 AI 生图 -> Ken Burns/混剪合成 -> 配音成片 -> 历史任务 -> 发布助手。
- 当前 2.0 的重点不是继续换模型，而是补齐“逐分镜素材编排层”和“发布助手浏览器”。

## 核心产品原则
- 用户看到的是成品工具，不是 API 调试页。
- Agent 配置放在“高级配置”，普通用户主要操作“新建内容、素材库、历史任务、发布中心”。
- 默认不能调用文生视频 t2v，避免继续消耗 `wan2.6-t2v` 额度。
- 发布助手不保存账号密码，不自动点击最终“发布”按钮。
- 所有 AI 生成图片都要自动入库并写入可复用元数据。

## 已确认事实
- 当前实际运行地址：`http://localhost:8888/`
- 旧项目目录：`/Users/zhouhan/Documents/agent-workflow`
- 2.0 项目目录：`/Users/zhouhan/Documents/agent-workflow-2.0`
- 最新核心文件：
  - `/Users/zhouhan/Documents/agent-workflow-2.0/index.html`
  - `/Users/zhouhan/Documents/agent-workflow-2.0/server.py`
  - `/Users/zhouhan/Documents/agent-workflow-2.0/compose_audio_video.swift`
  - `/Users/zhouhan/Documents/agent-workflow-2.0/douyin_publisher.py`
- 素材库已复制到：`/Users/zhouhan/Documents/agent-workflow-2.0/materials`
- 发布助手相关进度已复制：
  - `/Users/zhouhan/Documents/agent-workflow-2.0/.playwright_profile`
  - `/Users/zhouhan/Documents/agent-workflow-2.0/publish_tasks`
  - `/Users/zhouhan/Documents/agent-workflow-2.0/publish_debug`

## 已完成
- 前端已改为产品化工作台结构：
  - 新建内容
  - 素材库
  - 历史任务
  - 发布设置
  - 高级配置
- `wan2.6-t2v` 推荐文案已改为兼容项，不再误导用户。
- `apiFetch()` 中保留了 t2v 封堵：
  - 拦截 `video-generation/video-synthesis`
  - 区分 `kind=t2v / kind=i2v`
  - `kind=t2v` 直接阻止
  - `logger.error()` 记录到本地日志
- Ken Burns 主路径已跑通过 9:16 冒烟：
  - 输出 720x1280
  - 不触发 t2v
- 素材库 AI 自动入库图片已有轻量元数据：
  - `dimensions`
  - `orientation`
  - `aspectRatio`
  - `dominantColor`
- 前端素材卡片已展示宽高、方向、比例和主色。
- 历史任务代码路径存在：
  - `restoreTask()` 恢复成品预览
  - `downloadHistoryTask()` 导出 HTML 成品页
- 抖音发布助手方向已确认：
  - 只先做抖音
  - 自动上传和填表
  - 停在发布前确认页
  - 用户最后手动点发布

## 当前关键偏差
- 产品策略要求是：每个分镜先匹配本地素材/Pexels，缺口才 AI 生图。
- 当前代码实际仍偏向：只要有图片 Key，就先 `agent2_generateImages()` 全量生图。
- 这会导致生图慢、额度消耗多，也没有充分利用素材库。
- 不能改成“整片素材混剪失败后再生图”，那仍然太粗。
- 正确方向是“逐分镜素材决策”。

## 2.0 必须补的核心模块
### 1. Scene Asset Planner / 分镜素材编排器
输入：
- Agent1/导演输出的 `storyboard`
- 本地素材库 metadata/analysis
- Pexels 检索结果

输出示例：
```json
[
  {
    "sceneIndex": 0,
    "status": "matched",
    "source": "local",
    "assetUrl": "...",
    "score": 86,
    "reason": "命中办公室/团队/AI关键词"
  },
  {
    "sceneIndex": 1,
    "status": "missing",
    "source": "ai_image",
    "reason": "本地和Pexels都未找到客服系统相关素材"
  }
]
```

执行原则：
- 每个分镜单独匹配。
- 本地素材优先。
- Pexels 其次。
- 不合适或缺失的分镜才调用 AI 生图。
- AI 生图完成后立即自动入库并写元数据。
- 最终按分镜顺序交给 Ken Burns/混剪成片。

### 2. AI 生图自动入库修正
- 现在 AI 图入库主要发生在 `_resolve_image_path()` 被 Ken Burns 下载图片时。
- 如果 Ken Burns 失败或流程未走到下载，可能导致 AI 图没入库。
- 应新增显式接口，例如 `/material-import-image`：
  - 前端拿到 AI 图片 URL 后立即调用
  - 后端下载图片
  - 调用 `_add_image_to_materials()`
  - 返回素材项和 metadata

### 3. 发布助手浏览器
- 第一版只做抖音。
- 不保存账号密码。
- 使用受控浏览器 profile。
- 用户首次手动登录。
- 自动：
  - 打开抖音创作者上传页
  - 上传本地 mp4
  - 填标题
  - 填正文/话题
  - 停在发布前
- 不自动点击发布。
- 如果 Playwright/Chrome 不可用，要降级到半自动发布。

## 性能问题结论
- 最近一次生图慢，日志显示慢在通义万相排队：
  - 4 张图从 11:00:03 提交
  - 最后一张 11:04:03 完成
- 不是前端卡死，是 AI 生图服务排队/轮询慢。
- 曾出现 `Throttling.RateQuota`，说明一次性提交多张容易限流。
- 但暂时不减少生图张数，因为会影响视频质量。
- 优先通过“逐分镜素材匹配”减少不必要生图，而不是人为减少分镜图数量。

## HyperFrames 判断
- 当前会话没有可直接调用的 HyperFrames 工具。
- 如果 HyperFrames 指的是视频/帧级理解与编排能力，它最适合放在：
  - 分镜素材匹配层
  - 素材质量评分层
  - Agent4 成片验收层
- 它不应替代生图或视频合成，而应负责“看懂素材是否适合分镜”。

## 下一步建议
1. 先在 2.0 项目中实现 `Scene Asset Planner`。
2. 再补 `/material-import-image`，确保 AI 图生成后立即入库。
3. 然后再继续完善抖音发布助手。
4. 每次改完先跑最小冒烟：
   - 不触发 `kind=t2v`
   - 9:16 Ken Burns 可生成
   - AI 图入库有 metadata
   - 历史任务可恢复预览

