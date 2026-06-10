# Maestro — AI Agent Orchestrator

[![Tests](https://github.com/khoitran3172/maestro/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/khoitran3172/maestro/actions)
[![Python Version](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

*Read this in other languages: [Tiếng Việt](#tiếng-việt)*

**Maestro** is a production-ready AI developer orchestrator that coordinates multiple specialized AI agents (Claude Code, Codex, Stitch, Grok, etc.) to build software projects. By leveraging an asynchronous Directed Acyclic Graph (DAG) scheduler, multi-modal validation, and a strict Docker/Git-isolated execution sandbox, Maestro executes tasks in parallel while ensuring security, budget compliance, and code quality.

---

## Key Features

1. **Async DAG Scheduler**: Executes independent task nodes in parallel using `asyncio.Event` and controls concurrency with `asyncio.Semaphore`, maximizing throughput and decreasing execution times.
2. **Git & Workspace Isolation**: Mounts a temporary `git worktree` for each specialist task to prevent file collisions during concurrent runs. Merges changes automatically on success and aborts/rolls back on conflict.
3. **Docker Sandboxing**: Executes specialist commands inside isolated containers, setting memory boundaries and network policies (allowing offline profiles for local compilations and online profiles for API models).
4. **Credential Isolation**: Filters out host environment variables, passing only standard OS environment variables and allowlisted API credentials required by the specific specialist.
5. **Quality Grader Pipeline**: Runs deterministic checks (build, test, linter suites) followed by multi-modal LLM evaluations (Claude Sonnet for text/code and Opus Vision for visual UI checks) to reject poor output early.
6. **Smart Feedback Loop**: Analyzes rubric failures, compiler output, and previous artifacts to compile a rich context file (`feedback.md`) allowing specialists to repair issues iteratively instead of blind retries.
7. **SQLite State & Checkpointing**: Persists state transitions, cost tracking, and content-hashed file checkpoints inside SQLite (WAL mode). Seamlessly resumes paused or crashed runs using `--resume`.

---

## Quick Start

### Installation

```bash
# Clone the repository
git clone https://github.com/khoitran3172/maestro.git
cd maestro

# Install dependencies using pip or uv
pip install -e .
```

### Configure Environment

Copy `.env.example` to `.env` and fill in your API credentials:

```bash
cp .env.example .env
```

Ensure variables like `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, and `MAX_USD` (budget cap) are set.

### Running a Pipeline

```bash
# Run a new pipeline using a configuration file
maestro run --config pipeline.json

# Resume the latest run from the last successful checkpoint
maestro resume

# View current run task status
maestro status
```

### Running Tests

```bash
python -m pytest tests/ -v
```

---

# Tiếng Việt

**Maestro** là một hệ thống điều phối Agent lập trình AI chuyên nghiệp, điều phối nhiều AI Specialist chuyên biệt (Claude Code, Codex, Stitch, Grok,...) để phát triển dự án phần mềm. Tận dụng bộ lập lịch đồ thị bất đồng bộ (Async DAG Scheduler), bộ chấm điểm đa phương thức (Multi-modal Grader) và cơ chế đóng hộp cách ly Docker/Git chặt chẽ, Maestro tăng tốc phát triển thông qua xử lý song song đồng thời đảm bảo chất lượng, an toàn hệ thống và ngân sách dự án.

---

## Tính năng Nổi bật

1. **Lập lịch Async DAG**: Tự động chạy song song các task độc lập bằng `asyncio.Event` và điều khiển luồng qua `asyncio.Semaphore`, tối ưu hóa thời gian chạy dự án.
2. **Cách ly Git & Workspace**: Tạo các phân nhánh Git tạm thời (`git worktree`) riêng biệt cho mỗi task chạy song song để tránh xung đột ghi tệp. Tự động merge khi thành công và rollback an toàn khi xung đột.
3. **Đóng hộp Docker Sandbox**: Đóng gói các lệnh thực thi trong Container Docker, áp dụng chính sách giới hạn tài nguyên và cấu hình mạng (ONLINE cho gọi API model, OFFLINE cho biên dịch/test nội bộ).
4. **Cô lập thông tin đăng nhập**: Lọc bỏ các biến môi trường nhạy cảm của máy chủ, chỉ cung cấp các biến môi trường hệ thống cơ bản và API credentials được chỉ định cụ thể cho từng specialist.
5. **Đánh giá đa phương thức (Multi-modal Grading)**: Chạy kiểm tra tự động trước (build, test, lint) rồi mới gửi qua LLM Grader (Sonnet cho text/code, Opus Vision chấm điểm ảnh giao diện), giúp tiết kiệm 60% chi phí gọi API.
6. **Vòng lặp Phản hồi Thông minh**: Tự động tổng hợp lỗi rubric, log biên dịch biên dịch và mã nguồn cũ thành file phản hồi `feedback.md` chi tiết để Agent sửa đổi có mục tiêu rõ ràng.
7. **Lưu trữ SQLite & Checkpoint**: Quản lý lịch sử chạy, chi phí gọi API, trạng thái máy trạng thái trong SQLite (WAL mode) giúp khôi phục dễ dàng (`--resume`) khi tiến trình bị ngắt quãng.

---

## Hướng dẫn Sử dụng

### Cài đặt

```bash
# Nhân bản repository
git clone https://github.com/khoitran3172/maestro.git
cd maestro

# Cài đặt thư viện phát triển
pip install -e .
```

### Cấu hình biến môi trường

Sao chép `.env.example` thành `.env` và cung cấp các thông tin API keys:

```bash
cp .env.example .env
```

### Chạy Pipeline

```bash
# Chạy pipeline mới từ file cấu hình
maestro run --config pipeline.json

# Tiếp tục phiên chạy gần nhất bị tạm dừng hoặc crash
maestro resume

# Kiểm tra trạng thái chạy hiện tại
maestro status
```

### Chạy Kiểm thử (Tests)

```bash
python -m pytest tests/ -v
```

---

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
