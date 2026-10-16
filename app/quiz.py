"""Deterministic four-question RPG quiz; private state lives in the encrypted cashier."""
from urllib.parse import urlencode
from aiogram.types import InlineKeyboardButton,InlineKeyboardMarkup

QUESTIONS = (
    ('Вы очнулись в тёмном подземелье без памяти. Перед вами три двери:',
     ('Дверь с алыми рунами','Запечатанная цепями','Светящаяся золотом'),('shadow','tamer','saint')),
    ('Система выводит предупреждение: монстр ранга S приближается. Ваши действия:',
     ('Атаковать уязвимость','Подчинить зверя','Использовать магию молний'),('shadow','tamer','railgun')),
    ('Какую плату вы готовы отдать за абсолютную силу?',
     ('Одно воспоминание','Клятву святого','Жизнь двойника'),('shadow','saint','tamer')),
    ('Какое оружие формирует ваша аура?',
     ('Теневые клинки','Электромагнитный рельсотрон','Посох света','Кольцо укротителя'),
     ('shadow','railgun','saint','tamer')),
)
CLASSES = {'shadow':'Теневой Владыка','railgun':'Маг Рельсотрона',
           'saint':'Святой Инквизитор','tamer':'Великий Укротитель'}
MATCHES = {'shadow':('нулевого ранга','пробуждение'), 'railgun':('рельсотрон','молни'),
           'saint':('святой','инквизитор'), 'tamer':('прируч','укротител')}

def valid_answer(question: int, choice: int) -> bool:
    return type(question) is int and type(choice) is int and 0<=question<len(QUESTIONS) and 0<=choice<len(QUESTIONS[question][1])

def calculate_class(answers: str) -> str:
    if len(answers)!=4 or any(not c.isascii() or not c.isdigit() or not valid_answer(i,int(c)) for i,c in enumerate(answers)):
        raise ValueError('Four valid quiz answers required')
    scores={key:0 for key in CLASSES}
    for index,answer in enumerate(answers):
        scores[QUESTIONS[index][2][int(answer)]]+=2 if index==3 else 1
    weapon=QUESTIONS[3][2][int(answers[3])]
    winner=max(scores,key=lambda key:(scores[key],key==weapon))
    return CLASSES[winner]

def question_card(user: dict) -> tuple[str,InlineKeyboardMarkup]:
    index=len(user['quiz_answers'])
    prompt,choices,_=QUESTIONS[index]
    text=f'🔮 [ СИСТЕМА: ОПРЕДЕЛЕНИЕ СКРЫТОГО КЛАССА · {index+1}/4 ]\n\n{prompt}'
    rows=[[InlineKeyboardButton(text=choice,callback_data=f"qz:{user['quiz_session']}:{index}:{number}")]
          for number,choice in enumerate(choices)]
    return text,InlineKeyboardMarkup(inline_keyboard=rows)

def recommended_series(class_title: str, series: list[dict]) -> dict | None:
    if not series:return None
    key=next((key for key,title in CLASSES.items() if title==class_title),'shadow')
    terms=MATCHES[key]
    return max(series,key=lambda item:sum((len(terms)-i)*10 for i,term in enumerate(terms)
        if term in item['title'].casefold()))

def referral_text(class_title: str, username: str, tg_id: int) -> str:
    return (f'Я прошёл тест Системы и получил класс {class_title}! Узнай свой класс и читай бесплатно: '
            f'https://t.me/{username}?start=ref_{tg_id}')

def result_card(user: dict, series: dict | None, username: str, *, inline_enabled: bool=False) -> tuple[str,InlineKeyboardMarkup]:
    title=user['quiz_class_result']
    text=('🔮 [ РЕЗУЛЬТАТ ТЕСТА СИСТЕМЫ ]\n'
          f'👑 Ваш скрытый класс: {title}\n⚡ Ранг потенциала: S-ранг\n'
          f'📖 Ваша идеальная новелла: «{series["title"] if series else "Скоро в каталоге"}»\n'
          '🎁 Награда за прохождение: +50 XP к Рангу Игрока!\n'
          'Награда начисляется один раз. +50 XP — за каждого нового друга, открывшего главу 1 после первого входа по вашей ссылке.')
    rows=[]
    if series:
        rows.append([InlineKeyboardButton(text=f'📖 Начать читать «{series["title"]}»'[:100],callback_data=f'series:{series["id"]}')])
    share=referral_text(title,username,user['tg_id'])
    referral=f'https://t.me/{username}?start=ref_{user["tg_id"]}'
    share_url='https://t.me/share/url?'+urlencode({'url':referral,'text':f'🔮 Мой скрытый класс — {title}! Узнай свой класс в Системе.'})
    rows.append([InlineKeyboardButton(text='📤 Отправить другу (+50 XP за каждого)',
        **({'switch_inline_query':share} if inline_enabled else {'url':share_url}))])
    if inline_enabled:
        rows.append([InlineKeyboardButton(text='📤 Поделиться ссылкой',url=share_url)])
    rows.append([InlineKeyboardButton(text='👤 Мой Ранг в Системе',callback_data='profile')])
    return text,InlineKeyboardMarkup(inline_keyboard=rows)
