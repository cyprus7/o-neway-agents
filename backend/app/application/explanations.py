"""Saved-plan answers: deterministic evidence labels, no provider or model calls."""
from ..models import Session
from ..pricing.demo import build_demo_pricing


def saved_plan_answer(session: Session, action: str, option_ids: list[str]) -> str:
    available = {option.id: option for option in session.options}
    ids = list(dict.fromkeys(option_ids))
    if not ids:
        ids = list(available) if action == 'compare' else [session.selected_option_id] if session.selected_option_id else list(available)[:1]
    if not ids or len(ids) > 3 or any(item not in available for item in ids):
        return 'Укажите варианты из текущего результата поездки. Предыдущие версии доступны только для просмотра.'
    if action == 'compare' and len(ids) < 2:
        return 'Для сравнения нужны хотя бы два текущих варианта. Сейчас можно объяснить доступный вариант.'
    lines = ['Сравнение сохранённых вариантов.' if action == 'compare' else 'Объяснение сохранённого варианта.',
             'Новая проверка источников не выполнялась; наличие и условия без подтверждения остаются неизвестными.']
    for item in ids:
        option = available[item]
        lines.append(f'{option.destination} — {option.stay.title}')
        if option.reasons:
            lines.append('Основания рейтинга (оценка предпочтений): ' + '; '.join(option.reasons))
        for check in option.constraint_checks:
            label = {'passed': 'проверка пройдена', 'failed': 'нарушение', 'unknown': 'неизвестно'}.get(check.get('status'), 'неизвестно')
            lines.append(f"{check.get('constraint', 'Условие')}: {label}.")
        pricing = build_demo_pricing(session.intent, option)
        lines.append(f'Ориентировочно для демо: {pricing.total.min}–{pricing.total.max} {pricing.currency}; {pricing.scope_label}.')
        lines.append(pricing.budget_explanation)
        lines.extend('Компромисс: ' + value for value in option.tradeoffs)
        lines.extend('Ограничение: ' + value for value in option.risks)
        sources = list(dict.fromkeys(e.source_url for e in option.evidence if e.source_url))[:4]
        lines.extend('Сохранённый источник: ' + value for value in sources)
    return '\n'.join(lines)
