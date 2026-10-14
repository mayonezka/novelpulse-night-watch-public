"""Fixed Stars plans shared by invoices and payment validation."""

VIP_PLANS = {7: 49, 30: 149}


def parse_vip_payload(payload: str) -> tuple[int, int]:
    for days, price in VIP_PLANS.items():
        if payload == f"vip:{days}:{price}":
            return days, price
    raise ValueError("Некорректный тариф VIP. Откройте меню VIP и запросите новый счёт.")
