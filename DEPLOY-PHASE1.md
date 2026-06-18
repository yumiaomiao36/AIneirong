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
sudo apt install -y python3 python3-venv python3-pip nginx ffmpeg xvfb fluxbox x11vnc novnc websockify fonts-noto-cjk fonts-wqy-zenhei
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
PLAYWRIGHT_BROWSERS_PATH=/opt/agent-workflow-2.0/.playwright-browsers python -m playwright install chromium
mkdir -p .playwright_profile .playwright_runtime
sudo chown -R www-data:www-data data materials logs publish_tasks publish_debug .playwright-browsers .playwright_profile .playwright_runtime
```

## 云端可视化发布窗口（noVNC）

用于抖音发布 RPA 的云端浏览器可视化接管。先设置一个 VNC 密码：

```bash
cd /opt/agent-workflow-2.0
sudo x11vnc -storepasswd '请改成强密码' data/vnc.pass
sudo chown www-data:www-data data/vnc.pass
sudo chmod 600 data/vnc.pass
```

安装并启动虚拟桌面、VNC、noVNC：

```bash
sudo cp deploy/agent-workflow-display.service /etc/systemd/system/agent-workflow-display.service
sudo cp deploy/agent-workflow-window-manager.service /etc/systemd/system/agent-workflow-window-manager.service
sudo cp deploy/agent-workflow-vnc.service /etc/systemd/system/agent-workflow-vnc.service
sudo cp deploy/agent-workflow-novnc.service /etc/systemd/system/agent-workflow-novnc.service
sudo systemctl daemon-reload
sudo systemctl enable --now agent-workflow-display agent-workflow-window-manager agent-workflow-vnc agent-workflow-novnc
sudo systemctl status agent-workflow-novnc --no-pager
```

如果 `agent-workflow-novnc` 报 `status=203/EXEC`，检查：

```bash
which websockify
ls /usr/share/novnc
```

当前服务模板使用 `/usr/bin/websockify --web=/usr/share/novnc 0.0.0.0:6080 127.0.0.1:5900`。

浏览器访问：

```text
http://服务器公网IP:6080/vnc.html?autoconnect=true&resize=scale&shared=true
```

阿里云安全组需要临时放行 TCP `6080`。这是远程桌面入口，演示阶段建议只允许你的办公 IP 访问，不要长期全网开放。

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

## 服务器备份

备份会包含账号、系统配置、素材库、发布任务、日志、抖音登录态等关键数据；不会备份 `.venv` 和 Playwright 浏览器二进制。

```bash
cd /opt/agent-workflow-2.0
chmod +x deploy/backup-agent-workflow.sh
./deploy/backup-agent-workflow.sh
```

备份文件默认保存到：

```text
/opt/agent-workflow-backups/
```

默认自动保留最近 14 天。需要改保留天数时：

```bash
KEEP_DAYS=30 ./deploy/backup-agent-workflow.sh
```

## Phase 1 注意事项

- 已把前端接口从 `http://localhost:8888/...` 改成相对路径，云端访问不会请求客户自己的电脑。
- 服务监听地址支持环境变量：`HOST`、`PORT`。
- 云端建议启用 OSS，素材和成品不要长期堆在服务器磁盘。
- 抖音发布 RPA 在云端会打开服务器里的 Chromium；如需登录、验证码、人工确认发布，请通过 noVNC 发布窗口接管。
