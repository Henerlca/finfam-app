"""Rotas opcionais: cenários/metas/propostas, sem modificação automática de lançamentos."""
from __future__ import annotations
import json
import math
from datetime import date
from decimal import Decimal
from uuid import uuid4
from urllib.parse import urlencode
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from app.database import get_session_factory
from app.models import AppSetting
from app.repository import read_snapshot, cashflow_statuses, legacy_statuses, save_settings
from app.services.finance_core import (FAMILY, as_date, month_key, month_start, month_id, month_from_id, month_range,
                                     apply_payment_statuses, cashflow_items)
from app.services.horizon import effective_end
from app.services.family_planning import simulate_family, card_calendar, goal_progress
from app.services.charts import build_line_chart
from app.services.entry_review import read_review
from app.services.assistant_agent import sanitize_preferences
from app.routes import (PERSONS, render, checked_form, _snapshot, _selected_month, _person, _forecast,
                        _future_rows, _return_url, _chart_toggle_url, _assistant_context, parse_amount, _nav_context)

router = APIRouter()


def _positive_amount(raw, *, allow_zero=False):
    value = parse_amount(raw)
    if not math.isfinite(value) or value < 0 or (not allow_zero and value == 0):
        raise ValueError("Informe um valor positivo e válido.")
    return round(value, 2)


@router.get('/scenarios', response_class=HTMLResponse)
def family_scenarios_page(request: Request):
    txs,accounts,_,_,settings,closed = _snapshot()
    month,person = _selected_month(request,settings),_person(request)
    error = ''
    try:
        kind = request.query_params.get('kind','expense')
        if kind not in ('income','expense','savings'):
            raise ValueError('Escolha renda, consumo ou contribuição.')
        amount = _positive_amount(request.query_params.get('amount','0'),allow_zero=True)
        start = month_start(request.query_params.get('scenario_month') or month) or month
        duration = int(request.query_params.get('months','1'))
        if not 1 <= duration <= 240:
            raise ValueError('Escolha uma duração de 1 a 240 meses.')
    except (ValueError,TypeError,OverflowError) as exc:
        kind,amount,start,duration='expense',0.0,month,1
        error=str(exc)
    group = request.query_params.get('group','essential')
    if group not in ('essential','leisure'):
        group='essential'
    end=effective_end(txs,settings,month,[{'start_month':start,'months':duration}])
    direction = request.query_params.get('direction', 'increase')
    if direction not in ('increase', 'decrease'):
        direction = 'increase'
    full=_nav_context(request)['horizon']=='all'
    result=None
    if amount:
        signed_amount = -amount if direction == 'decrease' else amount
        result=simulate_family(txs,accounts,settings,closed,person,kind,signed_amount,start,duration,end)
        if direction == 'decrease':
            active = [r for r in result['alternative'] if r['scenario_active']]
            if ((kind == 'income' and any(r['income'] < 0 for r in active)) or
                    (kind == 'savings' and any(r['savings_net'] < 0 for r in active)) or
                    (kind == 'expense' and any(amount > float(r.get('fixed',0)) + float(r.get('debit_no_pot',0)) + float(r.get('debit_with_pot',0)) + float(r.get('card_total',0)) for r in active))):
                error = 'A redução ultrapassa o valor planejado em algum mês. Revise o valor ou a duração.'
                result = None
    if result:
        compare=_future_rows(result['compare'],month)
        chart_rows=compare if full else compare[:12]
        result['compare']=compare
        result['chart']=build_line_chart(chart_rows,[('base_ag','Cenário-base','#1b4d4b',False),('alternative_ag','Alternativa','#b47b35',False),('floor','Piso','#a9433d',True)],'family-scenario','Saldo bancário: base e alternativa',max_x_labels=5)
        last=chart_rows[-1] if chart_rows else None
        result['summary']={'month':last['month'] if last else None,'base_ending_balance':last['base_ag'] if last else None,'alternative_ending_balance':last['alternative_ag'] if last else None}
    return render(request,'family_scenarios.html','Cenários da família',month=month_key(month),person=person,people=PERSONS,
                  kind=kind,group=group,direction=direction,amount=amount,scenario_month=month_key(start),duration=duration,error=error,result=result,
                  chart_horizon='all' if full else '12',chart_toggle_url=_chart_toggle_url(request,'/scenarios',full))


@router.get('/cards/calendar', response_class=HTMLResponse)
def card_calendar_page(request: Request):
    txs,accounts,cards,_,settings,_=_snapshot()
    month,person=_selected_month(request,settings),_person(request)
    end=effective_end(txs,settings,month)
    context=_nav_context(request)
    full=context['horizon']=='all'
    months=month_range(month,end)
    visible=months if full else months[:12]
    statuses={}
    with get_session_factory()() as db:
        for current in visible:
            items=apply_payment_statuses(cashflow_items(txs,cards,accounts,current,person),legacy_statuses(db,current),cashflow_statuses(db,current))
            statuses[month_key(current)]={item['item_key']:{'paid':item['paid']} for item in items}
    all_calendar=card_calendar(txs,cards,visible,person,statuses)
    card_options=[{'name':card.get('name',''),'bank':card.get('bank','')} for card in all_calendar]
    requested_card=request.query_params.get('card','').strip()
    option_names={option['name'] for option in card_options if option['name']}
    card_filter=requested_card if requested_card in option_names else (card_options[0]['name'] if card_options else '')
    calendar=[card for card in all_calendar if card.get('name')==card_filter] if card_filter else all_calendar
    open_invoice_month=month_key(month_from_id((month_id(month) or 0)+1))
    sort_field=request.query_params.get('sort','date')
    if sort_field not in {'date','amount'}:
        sort_field='date'
    sort_direction=request.query_params.get('direction','desc')
    if sort_direction not in {'asc','desc'}:
        sort_direction='desc'
    for card in calendar:
        for row in card['months']:
            if row.get('paid'):
                row['status_label']='Baixa confirmada'
            elif row.get('month_key')==month_key(month):
                row['status_label']='Fatura do mês'
            elif row.get('month_key')==open_invoice_month:
                row['status_label']='Fatura aberta'
            else:
                row['status_label']='Prevista'
            for purchase in row.get('purchases',[]):
                purchase_date=as_date(purchase.get('transaction_date')) or month_start(purchase.get('start_month'))
                purchase['purchase_date_label']=purchase_date.strftime('%d/%m/%Y') if purchase_date else '—'
            def purchase_key(purchase):
                purchase_date=as_date(purchase.get('transaction_date')) or month_start(purchase.get('start_month')) or date.min
                value=Decimal(str(purchase.get('amount') or 0))
                return (purchase_date,value,str(purchase.get('description') or '').casefold(),int(purchase.get('id') or 0))
            row['purchases']=sorted(row.get('purchases',[]),key=purchase_key,reverse=sort_direction=='desc')
            row['details']=row['purchases']
        card['chart']=build_line_chart(card['months'],[('invoice','Fatura prevista','#2b6797',False),('parcelas','Parcelas','#89b5dc',True)],'card-selected','Faturas e parcelas previstas',max_x_labels=5)
    chart_toggle_url=_chart_toggle_url(request,'/cards/calendar',full)
    if card_filter:
        chart_toggle_url += ('&' if '?' in chart_toggle_url else '?')+urlencode({'card':card_filter})
    sort_params={'month':month_key(month),'person':person,'horizon':'all' if full else '12','card':card_filter}
    purchase_sort_urls={
        field:'/cards/calendar?'+urlencode({**sort_params,'sort':field,'direction':('asc' if sort_direction=='desc' else 'desc') if sort_field==field else 'desc'})
        for field in ('date','amount')
    }
    return render(request,'card_calendar.html','Próximas faturas',calendar=calendar,month=month_key(month),person=person,people=PERSONS,
                  card_options=card_options,card_filter=card_filter,open_invoice_month=open_invoice_month,
                  context_month=month_key(month),context_person=person,context_horizon='all' if full else '12',
                  horizon_months=len(visible),chart_horizon='all' if full else '12',chart_toggle_url=chart_toggle_url,
                  sort_field=sort_field,sort_direction=sort_direction,purchase_sort_urls=purchase_sort_urls)


@router.get('/goals', response_class=HTMLResponse)
def goals_page(request: Request):
    txs,accounts,cards,_,settings,closed=_snapshot()
    month,person=_selected_month(request,settings),_person(request)
    # Metas são familiares; filtros individuais não dividem alvos compartilhados.
    forecast=_forecast(txs,accounts,settings,closed,FAMILY,requested=month)
    categories=sorted({row['category'] for row in txs if not row['archived'] and row['type']=='Caixinha' and row.get('category')})
    paid={}
    first=month_start(settings.get('base_month')) or month
    current=date.today().replace(day=1)
    with get_session_factory()() as db:
        for m in month_range(first,current):
            items=apply_payment_statuses(cashflow_items(txs,cards,accounts,m,FAMILY),legacy_statuses(db,m),cashflow_statuses(db,m))
            paid[month_key(m)]={item['item_key']:{'paid':item['paid']} for item in items}
    goals=[{'goal':g,'progress':goal_progress(g,txs,paid,forecast,date.today(),accounts,cards)} for g in settings.get('family_goals',[]) or []]
    return render(request,'goals.html','Metas da família',goals=goals,categories=categories,month=month_key(month),person=person,people=PERSONS,
                  error=request.query_params.get('error',''))


@router.post('/goals/save')
async def goal_save(request: Request):
    form=await checked_form(request)
    try:
        with get_session_factory()() as db:
            txs,_,_,_,settings,_=read_snapshot(db)
            categories={row['category'] for row in txs if not row['archived'] and row['type']=='Caixinha'}
            category=str(form.get('category','')).strip()
            name=str(form.get('name','')).strip()
            if not name or len(name)>120 or category not in categories:
                raise ValueError('Informe um nome e uma categoria de Caixinha existente.')
            target=month_start(str(form.get('target_month','')))
            if not target or target < date.today().replace(day=1):
                raise ValueError('Escolha um prazo atual ou futuro.')
            opening_raw=str(form.get('opening_amount','')).strip()
            opening=_positive_amount(opening_raw,allow_zero=True) if opening_raw else None
            opening_month=month_start(str(form.get('opening_month',''))) if form.get('opening_month') else None
            if opening is not None and (not opening_month or opening_month > date.today().replace(day=1)):
                raise ValueError('Informe em que competência passada ou atual o saldo anterior foi apurado.')
            ident=str(form.get('id','')).strip()
            goals=settings.get('family_goals',[]) or []
            if ident and not any(g['id']==ident for g in goals):
                raise ValueError('Meta não encontrada; atualize a página.')
            goal={'id':ident or str(uuid4()),'name':name,'category':category,'target_amount':_positive_amount(form.get('target_amount')),
                  'target_month':target.isoformat(),'opening_amount':opening,'opening_month':opening_month.isoformat() if opening_month else None}
            # Uma categoria tem um único objetivo ativo, evitando dois alvos contarem o mesmo saldo.
            if any(g.get('category')==category and g['id']!=goal['id'] for g in goals):
                raise ValueError('Essa categoria já tem uma meta; edite-a em vez de duplicar o saldo.')
            goals=[g for g in goals if g['id']!=goal['id']]+[goal]
            save_settings(db,{'family_goals':goals})
            db.commit()
        return RedirectResponse(_return_url(request,'/goals',form),303)
    except (ValueError,TypeError) as exc:
        return RedirectResponse(_return_url(request,'/goals',form,error=str(exc)),303)


@router.post('/goals/{goal_id}/delete')
async def goal_delete(request: Request,goal_id:str):
    form=await checked_form(request)
    with get_session_factory()() as db:
        settings=read_snapshot(db)[4]
        goals=settings.get('family_goals',[]) or []
        if not any(g['id']==goal_id for g in goals):
            raise HTTPException(404,'Meta não encontrada.')
        save_settings(db,{'family_goals':[g for g in goals if g['id']!=goal_id]})
        db.commit()
    return RedirectResponse(_return_url(request,'/goals',form),303)


@router.post('/assistente/preferencias')
async def assistant_preferences(request: Request):
    form=await checked_form(request)
    prefs={} if form.get('reset') else sanitize_preferences({key:str(form.get(key,'')) for key in ('priorities','protected_goals','response_style')})
    with get_session_factory()() as db:
        save_settings(db,{'assistant_preferences':prefs})
        db.commit()
    return RedirectResponse(_return_url(request,'/assistente',form),303)


@router.post('/assistente/revisar',response_class=HTMLResponse)
async def assistant_review(request: Request):
    form=await checked_form(request)
    payload=read_review(str(form.get('proposal_token') or form.get('token') or ''))
    csrf=(getattr(request.state,'family_session',None) or {}).get('csrf')
    if not payload or payload.get('kind')!='ai-proposal' or payload.get('csrf')!=csrf:
        raise HTTPException(400,'Proposta expirada. Faça uma nova consulta antes de revisar.')
    proposal=payload.get('proposal') or {}
    action=proposal.get('action') or {}
    if action.get('kind')=='scenario':
        params={'kind':action['scenario_kind'],'amount':action['amount'],'scenario_month':action['start_month'],'months':action['duration'],
                'month':action['start_month'],'horizon':'12','person':FAMILY}
        return RedirectResponse('/scenarios?'+urlencode(params),303)
    if action.get('kind')!='reallocate':
        raise HTTPException(400,'Esta proposta não tem uma ação disponível.')
    context=_assistant_context(start_month=action.get('start_month'))
    suggestion=context['suggestion']
    start=context['start_month']
    if action.get('start_month') and start != action['start_month']:
        raise HTTPException(400,'A competência proposta não está aberta. Faça uma nova consulta; nenhum ajuste foi feito.')
    schedule=context['pot_reserve_schedule'].get(start,{})
    target=suggestion['target_pot']; source=suggestion['source_pot']
    increase=float(action.get('amount',0))
    target_before=float(schedule.get(target,suggestion['current_target']))
    source_before=float(schedule.get(source,suggestion['current_source']))
    protected = str(context.get('preferences',{}).get('protected_goals','')).casefold()
    if (not target or 'gasolina' not in target.casefold() or not source or 'lazer' not in source.casefold()
            or 'lazer' in protected or not math.isfinite(increase) or increase<=0 or increase>source_before):
        raise HTTPException(400,'O orçamento atual não permite revisar essa realocação. Nenhuma alteração foi feita.')
    context.update(ai_enabled=True,reviewed_proposal=proposal,form_values={'target_pot':target,'source_pot':source,'target_amount':round(target_before+increase,2),'source_amount':round(source_before-increase,2),'start_month':start},
                   month=start,person=FAMILY)
    nav = {'month':start,'person':FAMILY,'horizon':'12'}
    context.update(nav_context=nav, nav_query=urlencode(nav))
    return render(request,'assistant.html','Revisar proposta',**context)
