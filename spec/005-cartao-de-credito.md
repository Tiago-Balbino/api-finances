# Cartão de crédito, forma de pagamento, previsão e multa

## Forma de pagamento
- Despesas (lançamentos e despesas planejadas) têm forma de pagamento: `pix`, `debit` ou `credit`. Na despesa planejada ela é a forma prevista; o pagamento registra a forma usada.
- No crédito, a pessoa escolhe a conta e um cartão **dessa conta** (`payment_account_id`), além do número de parcelas (1–48).

## Cartão
- Cada cartão pertence a uma conta (que paga as faturas) e tem limite base, dia de fechamento, dia de vencimento e juros da fatura em atraso (% ao mês, aplicado ao dia).
- **Limite garantido**: ao criar o cartão é criada uma conta `Limite garantido`, que pode render por uma taxa de referência (mesmo motor dos rendimentos passivos). Aportes e resgates são transferências entre a conta escolhida e essa conta.
- Limite total = limite base + saldo do limite garantido. Usado = soma das faturas em aberto (inclui parcelas futuras e juros). Disponível = total − usado.
- Resgate não pode passar do saldo garantido nem deixar o disponível negativo.

## Compras e faturas
- Compra no dia do fechamento ou depois entra na fatura seguinte. Vencimento no mesmo mês do fechamento quando `dia_vencimento > dia_fechamento`; senão, no mês seguinte. Dias inexistentes viram o último dia do mês.
- Parcelas dividem o total em centavos; a sobra vai para a primeira. Todas ficam com a data da compra, cada uma ligada à fatura do seu mês.
- Cada fatura é uma despesa (`bills.credit_card_id`) paga por Pix ou débito. Ela não pode ser editada, excluída ou reclassificada.
- Compra em fatura que já tem pagamento não pode ser excluída.

## Saldo e relatórios (`transactions.movement_type`)
| tipo | saldo da conta | gastos/entradas |
|---|---|---|
| `regular` | sim | sim |
| `card_purchase` | não | sim, na data da compra |
| `invoice_payment` | sim | não |
| `transfer` | sim | não |

## Juros e multa
- O juro informado tem um período (ao dia, ao mês, ao ano) e uma aplicação (todo dia, todo mês, todo ano). A taxa é convertida proporcionalmente com dia = 1, mês = 30 e ano = 360 dias. Exemplo: 2% ao mês aplicado todo dia = 0,0667% por dia.
- Os juros são compostos e cobrados a cada período de aplicação completo após o vencimento.
- Multa opcional, cobrada uma única vez sobre o saldo em aberto quando o atraso chega a 1 dia, 1 mês ou 1 ano.

## Previsão (`/api/dashboard?month=` futuro e `/api/forecast`)
- Até 12 meses à frente. Despesas fixas e entradas fixas são criadas com 12 meses de antecedência.
- Nenhuma despesa em aberto é considerada paga: ela segue nos meses seguintes com juros e multa e não reduz o saldo.
- Saldo projetado = saldo atual das contas (sem limite garantido) + entradas previstas até o fim do mês. Sobra/falta = saldo projetado − acumulado a pagar.
