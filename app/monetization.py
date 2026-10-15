"""Fixed Stars plans shared by invoices and payment validation."""

VIP_PLANS = {1: 19, 7: 49, 30: 149}
VIP_LABELS = {
    1: '⚡ VIP на 24 часа — 19 ⭐️ (Хит на вечер!)',
    7: '👑 VIP на 7 дней — 49 ⭐️ (Выгодно)',
    30: '👑 VIP на 30 дней — 149 ⭐️',
}


def vip_duration(days: int) -> str:
    return '24 часа' if days == 1 else f'{days} дней'


def parse_vip_payload(payload: str) -> tuple[int, int]:
    if payload=='vip:1:9':return 1,9
    for days, price in VIP_PLANS.items():
        if payload == f"vip:{days}:{price}":
            return days, price
    raise ValueError("Некорректный тариф VIP. Откройте меню VIP и запросите новый счёт.")


def vip_options(user=None):
    options=[(VIP_LABELS[days],f'vip:{days}:{price}') for days,price in VIP_PLANS.items()]
    if user and user.get('player_xp',0)>=50:
        options.insert(0,('⚡ Секрет E-ранга: VIP на 24 часа — 9 ⭐️','vip:1:9'))
    return options
