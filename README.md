# NDA Sanitizer

Локальный конвейер для подготовки текста перед отправкой во внешнюю LLM. Сам
санитайзер ничего не отправляет наружу: единственный HTTP-вызов выполняется к
`llama-server` на `127.0.0.1`.

## Сначала заполните словарь

`config/nda_terms.yaml` является главным слоем защиты. Примеры в репозитории
полностью синтетические. Замените их реальными терминами своей организации и
сверьте допустимость такого процесса с политикой работодателя:

- `project_names` - названия проектов;
- `service_names` - внутренние сервисы и системы;
- `internal_domains` - внутренние домены;
- `db_schemas` - схемы и другие идентификаторы данных;
- `people` - имена;
- `business_terms` - уникальные бизнес-термины.

Не коммитьте заполненный словарь. Совпадения в словаре точные и чувствительны к
регистру. Добавляйте варианты написания отдельными строками.

## Архитектура

Обработка выполняется строго в таком порядке:

1. `detect-secrets` и встроенные сигнатуры блокируют текст при обнаружении
   потенциального секрета. Секреты не маскируются и не пропускаются дальше.
2. Термины из `config/nda_terms.yaml` заменяются стабильными плейсхолдерами.
3. Presidio со spaCy-моделями для английского и русского заменяет PII.
4. Qwen3-8B через локальный `llama-server` ищет пропущенные имена компаний,
   проектов и внутренних систем. В текст попадают только дословные подстроки из
   ответа модели.

Соответствие плейсхолдеров исходным значениям хранится как JSON в `vaults/` с
правами `0600`. Лог `logs/sanitizer.log` содержит только идентификаторы,
категории и количества, но не исходные значения.

## Установка

Системные зависимости Ubuntu:

```bash
sudo apt-get install -y build-essential cmake git python3-venv \
  libvulkan-dev vulkan-tools glslc mesa-vulkan-drivers spirv-headers
```

Python-окружение:

```bash
cd ~/nda-sanitizer
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e '.[test]'
.venv/bin/pip install 'torch>=2.4' --index-url https://download.pytorch.org/whl/cpu
.venv/bin/pip install llm-guard
.venv/bin/python -m spacy download en_core_web_lg
.venv/bin/python -m spacy download ru_core_news_lg
```

CPU-only индекс PyTorch не даёт llm-guard установить ненужный NVIDIA CUDA runtime.

Сервер устанавливается как user-unit:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/llama-sanitizer.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now llama-sanitizer.service
curl --fail http://127.0.0.1:8080/v1/models
```

Unit слушает только `127.0.0.1:8080`.

## CLI

Текст можно передать аргументом или через stdin:

```bash
cd ~/nda-sanitizer
.venv/bin/nda-sanitizer sanitize 'SecretProjectX belongs to Alex Example'
printf '%s' 'SecretProjectX belongs to Alex Example' | \
  .venv/bin/nda-sanitizer sanitize
.venv/bin/nda-sanitizer check 'SecretProjectX belongs to Alex Example'
.venv/bin/nda-sanitizer restore VAULT_ID '[PROJECT_1] belongs to [PERSON_1]'
```

`sanitize` печатает JSON с полями `clean_text` и `vault_id`. `restore` печатает
восстановленный текст. `check` не создаёт vault и показывает только категории,
количества и будущие плейсхолдеры, без исходных чувствительных значений.

## Python API

```python
from src.sanitizer import Sanitizer

sanitizer = Sanitizer()
clean_text, vault_id = sanitizer.sanitize(source_text)

# Здесь clean_text можно передать внешнему клиенту самостоятельно.
external_answer = call_external_model(clean_text)
restored_answer = sanitizer.restore(external_answer, vault_id)
```

Для сохранения byte-to-byte round trip внешняя система не должна менять ничего,
кроме допустимого форматирования плейсхолдеров. В ответах поддерживаются варианты
вроде `[ PROJECT _ 1 ]`.

## Проверки

```bash
cd ~/nda-sanitizer
.venv/bin/pytest
.venv/bin/pytest
```

Интеграционный LLM-тест требует запущенного `llama-server` и допускает один
повтор, поскольку классификация моделью вероятностная.

## Ограничения

- Серия отдельных очищенных запросов может в сумме раскрыть контекст
  (mosaic-эффект). Санитайзер не отслеживает смысл между vault-сессиями.
- Сетевые метаданные, учётная запись внешнего сервиса и характер активности не
  скрываются.
- LLM-слой вероятностный и не заменяет тщательно заполненный словарь.
- Presidio и секрет-сканеры дают как пропуски, так и ложные срабатывания.
- Vault содержит исходные значения в открытом виде на локальном диске. Защитите
  домашний каталог шифрованием, правами доступа и резервными копиями.
- Внешняя модель может удалить или сильно изменить плейсхолдер, после чего
  автоматическое восстановление станет невозможным.
- Перед использованием на рабочих данных необходимо согласование с политикой и
  юридическими требованиями работодателя.

