# Jarvis

A local-first voice and chat assistant powered by a language model, with a browser-based command console.

## Run Locally

Use Python 3.10 or newer and install Ollama. From the project root:

```powershell
python -m pip install -r requirements.txt
ollama pull qwen3-coder:30b
python app.py
```

The configured model is `qwen3-coder:30b` by default. It is a large download and needs substantial memory; choose a smaller Ollama model by setting `JARVIS_OLLAMA_MODEL` before starting Jarvis if your computer cannot run it. Ollama must be running locally.

Open [http://127.0.0.1:5000](http://127.0.0.1:5000). Flask binds to loopback only. General questions are sent to Ollama on the same machine. The browser console keeps the recent conversation context in the current page; it is not saved between sessions.

## On-Demand Web Search

The local model does not browse on its own. When you ask for current information, Jarvis sends that query to Brave Search and gives the returned snippets and source links to the local model. Configure a Brave Search API key in your Windows user environment as `BRAVE_SEARCH_API_KEY`, then restart Jarvis. The key is read by the server and is not sent to the browser or stored in this repository. `config/rules.json` controls whether external requests are allowed; internet access is enabled for the search and page-reading integrations.

To read a specific page, ask Jarvis to open/read/summarize it and include its `https://` URL. Page reading works without a Brave key; search requires one. Only public HTTPS websites on port 443 are fetched; private/local IPs, non-web content, and oversized pages are rejected. Redirects are revalidated. Page/search content is treated as untrusted input and shown with source links. This is on-demand search and page reading, not unrestricted browsing or access to arbitrary internet services.

## Saved Knowledge

Use the **Saved knowledge** panel to import files or remember an HTTPS page. Pages read in chat are added automatically. Supported files: TXT, Markdown, HTML, CSV, PDF, DOCX, PPTX, XLSX, and common image formats. Uploads are limited to 25 MB; PDFs are limited to 50 pages.

Jarvis stores extracted text and source metadata, not the original uploaded files, in `%LOCALAPPDATA%\JarvisCore\knowledge.sqlite3`. Set `JARVIS_KNOWLEDGE_DB` before launch to choose another database path. It uses local SQLite full-text search to retrieve relevant passages and cite their source in later answers; it does not retrain the language model. Saved excerpts are only sent to local Ollama, never to the optional hosted model. Use **Remove** beside a source to delete its extracted text and search index.

Text-based PDFs work without OCR. Images and scanned PDF pages use RapidOCR with bundled ONNX models and CPU inference; OCR runs locally, with no cloud service or separate Tesseract installation. The models are installed with the Python dependencies. OCR readiness is shown in the library panel.

## Home Assistant

Home Assistant is the device-control bridge. Jarvis does not discover or control every device on the LAN/WAN directly. After you install Home Assistant:

1. Create a Home Assistant long-lived access token and set `JARVIS_HA_URL` and `JARVIS_HA_TOKEN` in your Windows user environment. Use your local Home Assistant URL, such as `http://homeassistant.local:8123`; remote URLs must use HTTPS.
2. Add only approved entity IDs to `home_assistant.allowed_entities` in `config/rules.json`. It starts empty, so no device is controllable by default.
3. Restart Jarvis. The page will show the allowed entities and provide controls for lights, switches, fans, and climate entities. Each action prompts for confirmation and is checked against the entity and service allowlists again by the server.

The initial service allowlist permits light/switch/fan on/off and climate set-temperature only. Temperature values are limited to 5-35 C. Change these rules deliberately; locks, alarms, cameras, and arbitrary service calls are not enabled.

## Security Center

The **Run self-audit** action checks this Jarvis configuration and request guardrails locally; it does not scan network hosts or run exploits. **Audit packages** runs `pip-audit` against the installed Python environment and checks package names/versions with the public advisory service. It does not contact target hosts, upgrade dependencies, or change your system. Review findings before changing packages; the selected Python interpreter is a shared system environment, so results can include unrelated packages.

**Check headers** accepts one exact HTTPS URL after you confirm that you own or are authorized to assess it. It checks the TLS request result and common browser security headers only. It does not enumerate ports, scan IP ranges/CIDRs, attempt logins, or test exploits. A Kali SSH bridge is not configured; Jarvis does not execute arbitrary remote shell commands. The Security Center is defensive configuration review, not a penetration-testing or exploit framework.

## Code Workbench

The Code Workbench uses the local Ollama model to draft changes to existing files or propose new Lua, PHP, JavaScript, C/C++, and Python source files. It is scoped to `C:\Projects`, configured by `code_workbench.workspace_root` in `config/rules.json`; the folder was created for you. It displays a unified diff first. Applying requires a separate confirmation, backs up replaced files under `.jarvis_backups/`, and rejects stale drafts. New files are created only in existing subfolders and do not have an original-file backup. Generated code is never executed or tested automatically.

The Jarvis application tree is explicitly excluded from the workspace, so the workbench cannot edit its own running code, security rules, or device/network controls. It is not a general-purpose shell or autonomous self-modifier. Model output may be incorrect; review the entire diff before applying it.

Use **Edit hard rules** in the Jarvis page to edit `assistant_profile.hard_rules`; the text is saved into `config/rules.json` and applied to future local-model prompts. `assistant_profile.custom_instructions` remains available for style preferences. Webpages, search results, and quoted conversations are passed as untrusted data, not instructions. Your rules can steer Jarvis but cannot change the model's training or override fixed application/runtime safety limits.

## Hosted Provider (Optional)

Hosted inference sends prompts to the selected provider. It is disabled by default, even though on-demand search and page reading are enabled. To opt in to sending model prompts to a hosted provider, configure these environment variables, then restart Jarvis:

```powershell
$env:JARVIS_AI_PROVIDER = "openai-compatible"
$env:JARVIS_ALLOW_EXTERNAL_AI = "true"
$env:JARVIS_HOSTED_MODEL = "gpt-4o-mini"
```

Set `JARVIS_API_KEY` in your user environment without putting the key in this repository. The default endpoint is `https://api.openai.com/v1`; use `JARVIS_HOSTED_BASE_URL` for another HTTPS OpenAI-compatible endpoint. To return to local inference, set `JARVIS_AI_PROVIDER` to `ollama` or remove it.

## Capabilities And Limits

- Open-ended answers and multi-turn context from the configured language model.
- On-demand Brave search for current information; requires a locally configured API key.
- Optional Home Assistant control for explicitly allowlisted entities; requires a local URL/token and per-action confirmation.
- Local time/date answers and simulated light/thermostat controls remain available for testing.
- Browser speech input and spoken replies where supported. Speech recognition may use the browser vendor's service.
- The UI shows the active provider/model or when the model is unavailable. Hosted API keys stay server-side.