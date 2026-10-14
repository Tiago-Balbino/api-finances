# Páginas por mês, investimentos e desempenho

## Resumos por mês (`/api/bills/month`, `/api/incomes/month`, `/api/transactions/month`)
- Data de referência: hoje no mês atual; fim do mês em meses passados e futuros. Nada em aberto é considerado pago.
- Despesas: **a vencer** (em aberto, ainda não vencidas na referência), **atrasado** (em aberto e vencidas; em mês futuro, as que vêm de meses anteriores), **falta pagar** = a vencer + atrasado, **já pago** = pagamentos com data no mês. Fixas, eventuais e faturas são somadas separadamente pelo valor original das que vencem no mês. **Custo de vida** = fixas do mês, com o percentual sobre as entradas fixas previstas no mês.
- Entradas: recebido no mês (inclui rendimentos), rendimentos no mês, o que ainda vai entrar (com atrasadas), entradas fixas previstas e recebimentos por categoria.
- Lançamentos: só os avulsos (sem pagamentos de despesas, recebimentos, transferências e rendimentos automáticos); compra parcelada aparece uma vez pelo total.
- Painel (`/api/dashboard`): `debt` (tudo em aberto, inclusive faturas e parcelas futuras; vence até o fim do mês / depois; vencido), `bills_month`, `incomes_month` e `cards` (fatura em aberto mais próxima e limite).

## Investimentos
- Caixas têm movimentos (`investment_movements`): aportes e resgates, cada um rendendo pela taxa do caixa a partir da própria data. Caixas antigos recebem o valor inicial como primeiro aporte.
- Movimento com conta cria um lançamento `transfer` na conta: muda o saldo, não conta como gasto.
- `/api/investments/summary` soma caixas, contas com taxa de rendimento e limite garantido: total, rendimento acumulado, do mês, estimativa do próximo mês e projeção de 12 meses.

## Desempenho
- Repetições mensais: uma consulta lê os meses existentes e só os que faltam são inseridos, em lote.
- Rendimento automático: o saldo das 8h de cada dia é calculado em memória a partir de uma leitura dos movimentos da conta.
- Pool de conexões (`psycopg-pool`, `DB_POOL_SIZE`, padrão 4) e `schema.sql` aplicado só quando o conteúdo muda (`schema_meta`).
- Respostas serializadas com `json.dumps` e comprimidas com gzip. `/api/state` não traz mais lançamentos nem séries que as páginas buscam por mês.
- `vercel.json` fixa a função em `gru1` (São Paulo), perto do banco Supabase `sa-east-1`.
