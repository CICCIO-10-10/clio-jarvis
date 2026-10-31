# CLIO — Local-First Autonomous AI Assistant

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python: 3.10+](https://img.shields.io/badge/Python-3.10%2B-brightgreen.svg)](https://www.python.org/)
[![Architecture: Hybrid Local/Cloud](https://img.shields.io/badge/Architecture-Hybrid%20Local%2FCloud-orange.svg)](#architecture)
[![Hardware: GTX 1050 Ti Ready](https://img.shields.io/badge/Hardware-GTX%201050%20Ti%20Friendly-purple.svg)](#hardware-requirements)

> **CLIO** is not another web-bound chatbot wrapper. It is an open-source, local-first autonomous desktop companion equipped with digital eyes (local OCR & vision), hands (native OS UI automation), creative spatial capabilities (Blender 3D synthesis), and persistent episodic memory.

---

## 🌟 Vision

Most AI assistants today live behind isolated browser tabs or corporate walls. They can chat, but they cannot see what you see, touch your desktop, or act autonomously inside your daily workflow without exorbitant cloud bills or latency lag.

**CLIO bridges the physical PC and machine intelligence:**
- **Local-First & Private:** Sensitive workspace data, desktop control, and UI interactions execute directly on your machine.
- **Body on the PC, Brain in the Cloud (or Local):** CLIO decouples reasoning from physical execution. Heavy planning tasks route seamlessly to scalable cloud LLMs, while fast interactions and local actions execute on device.
- **Deterministic Safety:** Unchecked agent autonomy is dangerous. CLIO embeds a hard programmatic **Control Plane** that validates costs, permissions, and tool execution boundaries before a single pixel moves or a token is billed.

---

## ⚡ Key Features

### 👁️ Digital Eyes: Vision & Local OCR
- Real-time window inspection and screen analysis.
- Privacy-preserving on-device OCR and lightweight visual parsing without uploading unnecessary personal telemetry to third parties.

### 🖐️ Desktop Computer Use: Native UI Automation
- Seamless operating system control powered by `PyAutoGUI` and native system hooks.
- Mouse movement, keyboard typing, window focus management, and application launching with deterministic safety constraints.

### 🎨 3D Procedural Generation: Blender Automation
- Direct integration with **Blender** headless or GUI workflows.
- Synthesize 3D assets, execute procedural Python geometry scripts, and inspect renderings dynamically from natural language instructions.

### 🧠 Persistent Episodic Memory: The Scribe
- Long-term memory and workspace synchronization powered by **The Scribe**.
- Stores project decisions, context shifts, and previous executions so CLIO recalls context across days without resetting conversations.

### 🔀 Intelligent Hybrid Model Router
- **Fast Local Commands:** Low-latency local inferences handled by **Ollama** (e.g., Llama 3.2, Qwen Coder) directly on your GPU/CPU.
- **Complex Planning & Vision:** High-capacity cloud reasoning (Gemini, DeepSeek, Groq) triggered only when deep contextual comprehension is required.

### 🛡️ Cost Guard & Hardened Control Plane (`policy.py`)
- Real-time token budget tracking and rate-limiting.
- Strict policy enforcement: prevents prompt-injection escapes, unauthorized system deletions, recursive command loops, and surprise API bills.

---

## 🏗️ Architecture: "Cloud Reasoning + Local Body"

CLIO implements an asymmetric control loop designed for speed, safety, and frugality:

```
                  ┌──────────────────────────────────────────────┐
                  │                USER INTENT                   │
                  │        (Voice, Chat, System Event)           │
                  └──────────────────────┬───────────────────────┘
                                         │
                                         ▼
                  ┌──────────────────────────────────────────────┐
                  │                 MODEL ROUTER                 │
                  │   Evaluates complexity, latency & privacy    │
                  └──────────────┬────────────────┬──────────────┘
                                 │                │
            [Complex / Vision]   │                │   [Fast / System]
                                 ▼                ▼
                     ┌────────────────┐     ┌────────────────┐
                     │   Cloud LLM    │     │  Local Ollama  │
                     │ Gemini/DeepSeek│     │  Llama / Qwen  │
                     └────────┬───────┘     └────────┬───────┘
                              │                      │
                              └──────────┬───────────┘
                                         ▼
                  ┌──────────────────────────────────────────────┐
                  │         CONTROL PLANE (`policy.py`)          │
                  │  Cost Guard · Safety Bounds · Action Auth    │
                  └──────────────────────┬───────────────────────┘
                                         │  (Approved Actions Only)
                                         ▼
                  ┌──────────────────────────────────────────────┐
                  │              LOCAL SKILL ENGINE              │
                  ├──────────────────────┬───────────────────────┤
                  │  🖥️ Computer Use     │  🎨 Blender 3D        │
                  │  👁️ Local Vision/OCR │  📚 The Scribe Memory │
                  └──────────────────────────────────────────────┘
```

1. **Intake & Routing:** Prompts pass to the Model Router. Trivial commands never leave your machine; complex tasks route through secure cloud providers.
2. **Policy Interception:** Every planned action (mouse click, shell command, file write, API call) is screened by `policy.py` and `cost_guard.py`.
3. **Local Action Dispatch:** Authorized operations invoke local Python skills running in your local environment.
4. **Episodic Logging:** Outcomes are captured into The Scribe memory bank for continuous awareness.

---

## 💻 Hardware Requirements

You do **not** need a multi-thousand-dollar workstation to run CLIO. Because heavy cognitive loads can be offloaded to cloud endpoints while local agents remain lightweight:

| Component | Minimum Requirement | Recommended |
|---|---|---|
| **GPU** | NVIDIA GTX 1050 Ti (4GB VRAM) or Integrated CPU | NVIDIA RTX 3060 (12GB) or Apple Silicon M-Series |
| **RAM** | 8 GB System RAM | 16 GB+ System RAM |
| **Storage** | 5 GB free disk space | SSD with 20 GB free (for local LLM weights) |
| **OS** | Windows 10/11, Linux, or macOS | Windows 11 (fully tested with PyAutoGUI & Win hooks) |
| **Python** | Python 3.10 or 3.11+ | Python 3.11 |

*Note: With an entry-level card like the GTX 1050 Ti, CLIO utilizes quantized 3B local models for instantaneous local tasks, while querying Gemini or DeepSeek APIs for heavy contextual reasoning.*

---

## 🚀 Quickstart & Installation

### 1. Clone the Repository
```bash
git clone https://github.com/your-username/clio.git
cd clio
```

### 2. Create and Activate a Virtual Environment
```bash
python -m venv venv

# On Windows:
venv\Scripts\activate

# On Linux/macOS:
source venv/bin/activate
```

### 3. Install Dependencies
```bash
pip install -r requirements.txt
```

### 4. Configure Environment
Copy the example environment configuration and supply your keys:
```bash
cp .env.example .env
```
Edit `.env`:
```ini
# Cloud Reasoning (Optional if running 100% local)
GEMINI_API_KEY="your-gemini-api-key"
DEEPSEEK_API_KEY="your-deepseek-api-key"

# Local Inference (Optional, for offline execution)
OLLAMA_URL="http://127.0.0.1:11434"
OLLAMA_MODEL="llama3.2:3b"
```

### 5. Launch CLIO
```bash
python run_clio.py
```

---

## 🤝 Roadmap & Call for Contributors & Investors

We are building the future of **sovereign, local-first artificial personal intelligence** — a digital companion that respects user privacy, costs almost nothing at idle, and interacts with software tools just like a human does.

### We are actively looking for:
- 🛠️ **Open Source Contributors:** Help us expand native OS toolkits (Linux X11/Wayland & macOS accessibility bridges), refine local OCR accuracy, and build new modular skills.
- 💡 **AI Researchers & Prompt Engineers:** Optimize sub-3B parameter local models for structured JSON tool-calling and autonomous agent workflows.
- 🚀 **Designers & Technical Writers:** Elevate our documentation, interface design, and community guides.
- 💼 **Angel Investors & Sponsors:** Accelerate the transition from experimental laboratory architecture to a battle-tested desktop ecosystem.

If you are passionate about local AI autonomy, deterministic safety planes, and agentic workflows:
- Star the repository and join the discussion in **Discussions / Issues**.
- Reach out directly to collaborate or support the project: **maintainer@clioproject.org** (or open an issue).

---

## 📄 License

This project is licensed under the terms of the [MIT License](LICENSE).
Distributed freely for the community to study, hack, and expand.
