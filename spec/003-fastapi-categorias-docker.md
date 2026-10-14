# SDD 003 — FastAPI, categorias e Docker

## Objetivo

Migrar a API para FastAPI, mantendo o contrato `/api/*`, os dados do PostgreSQL e as regras de despesas e investimentos. Permitir classificação por categoria e subcategoria e observações em contas, despesas, lançamentos e investimentos.

## Regras

1. Categorias pertencem a entradas ou despesas. Subcategorias pertencem a uma categoria. Todos os usuários autenticados compartilham e gerenciam o catálogo.
2. Categoria é obrigatória em novos lançamentos e despesas; subcategoria é opcional. A API verifica que ambas combinam com o tipo do registro. Nomes são únicos sem diferenciar maiúsculas e minúsculas dentro do respectivo grupo.
3. A migração cria categorias a partir dos textos já salvos e preserva os nomes antigos. Categorias e subcategorias em uso não podem ser excluídas. Renomear altera o agrupamento exibido sem apagar lançamentos.
4. Observações são opcionais, limitadas a 2000 caracteres. Despesas com pagamentos podem ter as observações alteradas sem mexer no histórico financeiro.
5. O painel agrupa **despesas efetivamente pagas** no mês atual por categoria e subcategoria. Contas futuras ou pendentes entram na projeção de dívida, não no total de gastos realizados.
6. A imagem Docker da API usa FastAPI e PostgreSQL 16 via Compose. O banco usa volume nomeado. Uma importação explícita transfere os dados do PostgreSQL local antigo para o banco do Compose.

## Critérios de aceitação

- Categorias antigas aparecem no catálogo e seus lançamentos continuam classificados.
- Ao selecionar uma categoria, somente suas subcategorias aparecem no formulário; IDs incompatíveis são recusados pela API.
- Renomear uma categoria reflete nos gráficos e registros; excluir uma categoria em uso retorna conflito.
- O gráfico do mês soma pagamentos e saídas imediatas, sem contar despesas ainda não pagas.
- A API mantém login e sessões existentes após importação do banco antigo.
- `docker compose up -d --build` inicia banco e API e `/health` responde com a versão 3.
