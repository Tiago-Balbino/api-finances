# Rendimentos passivos e despesas por mês

- Taxas de referência têm nome e valor efetivo anual. Uma conta escolhe uma taxa e o percentual dela (por exemplo, 105%).
- O rendimento de cada dia útil usa o saldo disponível às 08:00 em America/Sao_Paulo, inclusive rendimentos anteriores e lançamentos registrados antes desse horário. A taxa anual efetiva da conta é a taxa de referência multiplicada pelo percentual escolhido; a taxa diária é `(1 + taxa_anual)^(1/252) - 1`.
- O crédito aparece como entrada na categoria Investimentos. Dias com valor arredondado a zero também são registrados no controle interno, evitando créditos retroativos após mudanças de saldo. O calendário exclui fins de semana e feriados bancários nacionais (datas fixas, Carnaval, Sexta-Feira Santa e Corpus Christi).
- Novas configurações feitas antes das 08:00 em America/Sao_Paulo começam no mesmo dia; depois das 08:00, no dia seguinte. Alterações de taxa são aplicadas apenas aos dias ainda não creditados.
- O endpoint `/api/cron/passive-yields` exige `Authorization: Bearer $CRON_SECRET`. Configure `CRON_SECRET` no projeto da API na Vercel. `vercel.json` agenda uma chamada às 11:00 UTC em dias úteis; `/api/state` recupera dias perdidos a partir das 08:00 locais. O plano Hobby da Vercel pode atrasar a execução do cron em até 59 minutos, então o crédito não é garantido exatamente às 08:00 nesse plano.
- Cada mês da visão de despesas inclui as contas daquele mês e as anteriores ainda em aberto. O vencimento original não é alterado, e juros seguem sendo calculados desde essa data.
