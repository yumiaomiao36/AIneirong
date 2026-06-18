# Agent Workflow 2.0 云端部署 Phase 1

目标：先让客户可通过电脑、手机、iPad 打开网页使用系统，后端运行在云服务器上。

## 服务器建议

- Ubuntu 22.04 / 24.04
- 4 核 CPU 起步，建议 8 核
- 8GB 内存起步，建议 16GB
- 100GB 云盘起步
- 带宽 5Mbps 起步，演示建议 10Mbps+

## 系统依赖

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip nginx ffmpeg fonts-noto-cjk
```

## 项目目录

推荐放到：

```bash
/opt/agent-workflow-2.0
```

需要保留的数据目录：

- `data/`：账号、次数、系统配置
- `materials/`：本地素材库
- `logs/`：运行日志
- `publish_tasks/`：发布任务状态
- `publish_debug/`：发布调试截图

## Python 环境

```bash
cd /opt/agent-workflow-2.0
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

## 本地测试启动

```bash
HOST=0.0.0.0 PORT=8888 python3 server.py
```

浏览器访问：

```text
http://服务器公网IP:8888
```

## systemd 托管

```bash
sudo cp deploy/agent-workflow.service /etc/systemd/system/agent-workflow.service
sudo systemctl daemon-reload
sudo systemctl enable agent-workflow
sudo systemctl start agent-workflow
sudo systemctl status agent-workflow
```

## Nginx 反向代理

先把 `deploy/nginx-agent-workflow.conf` 里的 `app.example.com` 改成你的域名。

```bash
sudo cp deploy/nginx-agent-workflow.conf /etc/nginx/sites-available/agent-workflow
sudo ln -s /etc/nginx/sites-available/agent-workflow /etc/nginx/sites-enabled/agent-workflow
sudo nginx -t
sudo systemctl reload nginx
```

然后访问：

```text
http://你的域名
```

正式对外建议再配置 HTTPS 证书。

## 后台配置

登录 `/admin` 后配置：

- 文案模型 Key
- 百炼 Key
- OSS AccessKey / Bucket / Endpoint
- 默认音色
- 客户账号和免费次数

## Phase 1 注意事项

- 已把前端接口从 `http://localhost:8888/...` 改成相对路径，云端访问不会请求客户自己的电脑。
- 服务监听地址支持环境变量：`HOST`、`PORT`。
- 云端建议启用 OSS，素材和成品不要长期堆在服务器磁盘。
- 抖音半自动发布 RPA 在云端会打开服务器里的 Chromium，客户未必能看到浏览器窗口；Phase 1 建议先把“生成/素材/账号/后台”跑通，发布助手后续单独做远程浏览器或本地发布助手。
