"""Owner-approved Stage 51 comments and exact analytics target selection."""
from app.privacy import assert_public_safe

PRIORITY = ('_0Nh9lmo1Yk', '8HFDej89gTk', 'UGhRfR_bwkM')
COMMENT_HOOKS = {
    '_0Nh9lmo1Yk': (1, 'Система показала ему собственную могилу. Это предупреждение — или приказ? Вы бы стали искать того, кто её выкопал?'),
    '8HFDej89gTk': (1, 'Ему присвоили нулевой ранг — почему тогда Система испугалась? Что опаснее: быть слабейшим или оказаться ошибкой в её правилах?'),
    'UGhRfR_bwkM': (1, 'А если нулевой ранг — не приговор, а способ спрятать силу? Как бы вы поступили: раскрыли секрет или продолжили притворяться слабым?'),
    'b5yPw4_IfaM': (3, 'Обычная трава принесла награду Системы. Но какой росток заставит её пожалеть о следующем шаге? Вы бы рискнули вырастить его?'),
    'kt6TQrMU5nE': (6, 'Система выдала класс укротителя. А если правила приручения работают в обе стороны? Кто в этой истории на самом деле хозяин?'),
    'IhQXaGLjH1A': (4, 'Тысячи людей попали в мир разломов, классов и уровней. Вы бы первым делом искали союзников или скрывали свой навык?'),
    'ebkx1VsUFwg': (5, 'Маги бросают молнии, а он решил собрать рельсотрон. Что победит: древнее заклинание или физика, которую этот мир ещё не знает?'),
    'SJdo6tPI3L0': (3, 'Ни меча, ни легендарного класса — только горсть семян. Можно ли обойти героев, если сама Система награждает тебя за каждый росток?'),
}


def stage51_targets(report):
    rows = [row for row in report['videos'] if row['type'] == 'Short' and row['comments'] == 0]
    if len(rows) != 8 or {row['video_id'] for row in rows} != set(COMMENT_HOOKS):
        raise ValueError('Stage 51 inventory differs from the approved eight Shorts; audit before sending')
    order = {vid: index for index, vid in enumerate(PRIORITY)}
    return sorted(rows, key=lambda row: order.get(row['video_id'], len(PRIORITY)))


def comment_copy(video_id):
    sid, hook = COMMENT_HOOKS[video_id]
    text = (hook + '\n\n📖 Первые главы бесплатно, продолжение истории — в Telegram. '
            'Код поиска: NovelPulseBot\n' + f'https://t.me/NovelPulseBot?start=series_{sid}')
    return assert_public_safe(text)
