"""Export one booking journal without connecting to Telegram or an LLM."""
import argparse
from pathlib import Path
from .store import StateStore


def main():
    parser = argparse.ArgumentParser(description='Экспорт журнала конкретной брони')
    parser.add_argument('directory', type=Path, help='Папка бронирования с messages.jsonl')
    args = parser.parse_args()
    if not (args.directory / 'messages.jsonl').is_file():
        parser.error('В папке нет messages.jsonl. Старый decisions.jsonl не преобразуется автоматически.')
    store = StateStore(args.directory / 'state.json', args.directory / 'decisions.jsonl')
    print(store.export_text())


if __name__ == '__main__':
    main()
