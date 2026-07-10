# Итоговый отчёт

Дата: 2026-07-10
Проект: `/home/dima/nda-sanitizer`

## Результат

Локальный NDA-санитайзер развёрнут и проверен. Конвейер сам не отправляет
данные во внешние API. Единственный сетевой вызов во время обработки направлен
на `http://127.0.0.1:8080` к локальному `llama-server`.

## Версии

- Ubuntu 24.04.4 LTS, kernel-драйвер GPU: `amdgpu`.
- GPU: AMD Radeon RX 6600, Navi 23, 8 ГБ.
- Vulkan instance 1.3.275, RADV Mesa 25.2.8.
- GCC 13.3.0, CMake 3.28.3, shaderc/glslc 2023.8.
- llama.cpp commit `67776eaee549be9e1e0359726c13c399b9224d2e`.
- Python 3.12.3, spaCy 3.8.13.
- Presidio analyzer/anonymizer 2.2.358.
- llm-guard 0.3.16, PyTorch 2.13.0+cpu.
- detect-secrets 1.5.0 и bc-detect-secrets 1.5.43.
- en_core_web_lg 3.8.0, ru_core_news_lg 3.8.0.
- pytest 8.4.2.

## Модель

- Репозиторий: `Qwen/Qwen3-8B-GGUF`.
- Файл: `models/Qwen3-8B-Q4_K_M.gguf`.
- Размер: 5 027 783 488 байт.
- SHA-256: `d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785`.
- Параметры сервера: `-ngl 99 -c 8192 --host 127.0.0.1 --port 8080`.
- Prompt processing: 57.83 ток/с.
- Генерация: 42.16 ток/с.
- Снижение context или GPU layers не потребовалось.

## Гейты

- Preflight: RX 6600/RADV найдена, 62 ГиБ RAM, 683 ГиБ свободно.
- Vulkan: `vulkaninfo --summary` показывает `AMD Radeon RX 6600 (RADV NAVI23)`.
- Сборка: Vulkan backend включён, `llama-server --version` работает.
- Модель: размер и SHA-256 совпадают с опубликованными.
- Сервис: `llama-sanitizer.service` имеет состояния `enabled` и `active`.
- API: `/v1/models` и chat-completion отвечают.
- Presidio: синтетические email, имя и IP обнаруживаются и восстанавливаются.
- Secret stop: синтетический AWS access key блокируется до замены.
- CLI: dictionary, PII и LLM-слой прошли сквозной sanitize/restore.
- Vault: JSON создаётся с правами `0600`; каталог имеет права `0700`.
- Логи: исходные заменённые значения в логах отсутствуют.
- Pytest, прогон 1: `10 passed in 7.72s`.
- Pytest, прогон 2: `10 passed in 6.71s`.

## Выборы на развилках

- Для актуального llama.cpp дополнительно установлен `spirv-headers`.
- Выбран официальный Qwen3-8B GGUF. Qwen3.5-9B новее, но выходит за точный
  8B-класс и официальный репозиторий не предоставляет требуемый GGUF.
- llm-guard установлен с CPU-only PyTorch, чтобы не ставить NVIDIA CUDA 13
  на AMD-машину. Инференс основного LLM использует только llama.cpp/Vulkan.
- Шумные entropy/keyword/public-IP плагины secret scanner отключены: они
  блокировали синтетические NDA-названия и IP до маскирования. Оставлены
  детекторы конкретных ключей/токенов и сигнатура присвоенного credential.

## Действия пользователя

1. Заменить синтетические примеры в `config/nda_terms.yaml` реальными терминами.
2. Не коммитить заполненный словарь и vault-файлы.
3. Сверить весь процесс и допустимые внешние сервисы с политикой работодателя.
4. Определить срок хранения vault-файлов и обеспечить шифрование диска/backup.
5. Учитывать mosaic-эффект: серия очищенных запросов может раскрыть контекст.
