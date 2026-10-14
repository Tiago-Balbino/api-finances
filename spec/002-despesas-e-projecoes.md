# SDD 002 — Despesas futuras e projeções diárias

## Objetivo

Planejar contas a pagar, acompanhar dívidas vencidas e registrar pagamentos sem alterar o saldo bancário antes do pagamento real. Mostrar investimentos e dívidas com valor atualizado a cada dia.

## Regras

1. Uma despesa planejada tem conta prevista, descrição, categoria, valor original, vencimento e juros opcionais. Os juros são uma taxa percentual diária ou mensal aplicada de forma composta somente após o vencimento.
2. O status é calculado na data atual: **pendente** antes ou no vencimento com saldo aberto; **vencida** depois do vencimento com saldo aberto; **paga** quando o saldo chega a zero. Pagamentos parciais mantêm o saldo aberto.
3. Registrar um pagamento cria um lançamento de saída na conta escolhida. O saldo da conta só diminui quando esse pagamento é registrado.
4. Pagamentos são aceitos até a data atual, em ordem cronológica e até o saldo devido naquela data. A diferença entre valor original e valor pago após o vencimento representa juros.
5. Valor dos investimentos e saldo devedor são calculados na leitura usando a data local configurada por `APP_TIMEZONE` (padrão `America/Araguaina`). Taxas mensais e anuais acumulam frações de período a cada dia.
6. A projeção de dívida mostra quanto seria devido se nenhum novo pagamento fosse registrado. A projeção de vencimentos agrupa o saldo aberto por mês. São cenários brutos, sem novos rendimentos, despesas ou recebimentos não cadastrados.

## Critérios de aceitação

- Uma despesa de R$ 100,00 que vence amanhã aparece pendente e não altera o saldo da conta.
- Um dia após o vencimento, a mesma despesa com juros de 1% ao dia aparece vencida com R$ 101,00 de saldo.
- Um pagamento de R$ 40,00 reduz a conta em R$ 40,00 e deixa R$ 61,00 em aberto naquele dia.
- Quitar o restante muda o status para paga e mantém o histórico dos pagamentos.
- Investimentos com taxa mensal apresentam valor acumulado diário; a projeção de 12 meses preserva a taxa composta contratada.
- A migração preserva contas, lançamentos e investimentos já existentes.
