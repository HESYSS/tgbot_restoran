"""Explicit API smoke test with fictional data; no Telegram interaction."""
import asyncio
import os
import httpx
from dotenv import load_dotenv
from .models import BookingRequest
from .providers import GroqProvider


async def main():
    load_dotenv()
    if not os.environ.get('GROQ_API_KEY', '').strip():
        print('Добавьте GROQ_API_KEY в локальный .env. Не отправляйте ключ в чат.')
        return
    request = BookingRequest('Fictional Test Bistro', '2030-01-15')
    try:
        reply = await GroqProvider().draft_reply(request, [], None)
    except httpx.HTTPStatusError as error:
        status = error.response.status_code
        messages = {401: 'Ключ отклонён. Проверьте GROQ_API_KEY.',
                    403: 'Нет доступа к выбранной модели или проекту.',
                    404: 'Модель недоступна. Проверьте GROQ_MODEL.',
                    429: 'Лимит исчерпан. Проверьте квоту в Groq Console и повторите позже.'}
        print(messages.get(status, f'Groq вернул HTTP {status}.'))
        return
    except httpx.RequestError:
        print('Не удалось связаться с Groq. Проверьте соединение и повторите позже.')
        return
    except (KeyError, IndexError, AttributeError, TypeError, ValueError):
        print('Groq вернул неожиданный ответ. Секреты и тело ответа не выведены.')
        return
    if not reply:
        print('Модель вернула пустой ответ. Попробуйте позже.')
        return
    print('Groq подключён. Ответ на вымышленную заявку:')
    print(reply)


if __name__ == '__main__':
    asyncio.run(main())
