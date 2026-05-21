import os
from dotenv import load_dotenv
import psycopg2
from typing import Optional, List
from langchain.tools import tool
from pydantic import BaseModel, Field

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

def get_conn():
    return psycopg2.connect(DATABASE_URL)

TYPE_ALIASES = {
    "INCOME": 'INCOME', "ENTRADA": "INCOME", "RECEITA": "INCOME", "SALÁRIO": "INCOME", "LUCRO": "INCOME",
    "EXPENSE": "EXPENSES", "EXPENSES": "EXPENSES", "DESPESA": "EXPENSES", "GASTO": "EXPENSES",
    "TRANSFER": "TRANSFER", "TRANSFERÊNCIA": "TRANSFER", "TRANSFERENCIA": "TRANSFER",
}

# Essa classe garante que o objeto de Python passe todos esses campos
class AddTransactionArgs(BaseModel):
    amount: float = Field(..., description="Valor da transação (use positivo).")
    source_text: str = Field(..., description="Texto original do usuário.")
    description: str = Field(..., description="Descrição para a transação.")
    occurred_at: Optional[str] = Field(
        default=None,
        description="Timestamp ISO 8601; se ausente, usa NOW() no banco."
    )
    type_id: Optional[int] = Field(default=None, description="ID em transaction_types (1=INCOME, 2=EXPENSES, 3=TRANSFER).")
    type_name: Optional[str] = Field(default=None, description="Nome do tipo: INCOME | EXPENSES | TRANSFER.")
    category_id: Optional[int] = Field(default=None, description="FK de categories (opcional).")
    category_name: Optional[str] = Field(default=None, description="Nome da categoria, caso não saiba o id, procure os nomes: comida, besteira, estudo, férias, transporte, moradia, saúde, lazer, contas, investimento, presente, outros")
    payment_method: Optional[str] = Field(default=None, description="Forma de pagamento (opcional).")

class SaldoDiarioArgs(BaseModel):
    date_local: str = Field(..., description="Dia em que ocorreu as transações.")

class SearchTransactionArgs(BaseModel):
    text: Optional[str] = Field(default=None, description="Palavra chave para procurar dentro das descrições")
    type_id: Optional[int] = Field(default=None, description="ID em transaction_types (1=INCOME, 2=EXPENSES, 3=TRANSFER).")
    type_name: Optional[str] = Field(default=None, description="Nome do tipo: INCOME | EXPENSES | TRANSFER.")
    category_id: Optional[int] = Field(default=None, description="FK de categories (opcional).")
    category_name: Optional[str] = Field(default=None, description="Nome da categoria, caso não saiba o id, procure os nomes: comida, besteira, estudo, férias, transporte, moradia, saúde, lazer, contas, investimento, presente, outros")
    init_date: Optional[str] = Field(
        default=None,
        description=(
            """Data de início (YYYY-MM-DD) em São Paulo.
            Para consultas sobre o FUTURO (compromissos, contas a pagar, etc.),
            defina como a data de hoje. "
            Para consultas sobre o PASSADO ou um intervalo específico, defina normalmente."""
        )
    )
    end_date: Optional[str] = Field(
        default=None,
        description=(
            """Data final (YYYY-MM-DD) em São Paulo.
            Para consultas sobre o FUTURO, deixe como None (sem limite).
            Para consultas sobre o PASSADO sem data final explícita, use hoje como padrão."""
        )
    )
    payment_method: Optional[str] = Field(default=None, description="Forma de pagamento a ser filtrado(opcional).")

class UpdateTransactionArgs(BaseModel):
    id: Optional[int] = Field(
        default=None,
        description="ID da transação a atualizar. Se ausente, será feita uma busca por (match_text + date_local)."
    )
    match_text: Optional[str] = Field(
        default=None,
        description="Texto para localizar transação quando id não for informado (busca em source_text/description)."
    )
    date_local: Optional[str] = Field(
        default=None,
        description="Data local (YYYY-MM-DD) em America/Sao_Paulo; usado em conjunto com match_text quando id ausente."
    )
    amount: Optional[float] = Field(default=None, description="Novo valor.")
    type_id: Optional[int] = Field(default=None, description="Novo type_id (1/2/3).")
    type_name: Optional[str] = Field(default=None, description="Novo type_name: INCOME | EXPENSES | TRANSFER.")
    category_id: Optional[int] = Field(default=None, description="Nova categoria (id).")
    category_name: Optional[str] = Field(default=None, description="Nova categoria (nome).")
    description: Optional[str] = Field(default=None, description="Nova descrição.")
    payment_method: Optional[str] = Field(default=None, description="Novo meio de pagamento.")
    occurred_at: Optional[str] = Field(default=None, description="Novo timestamp ISO 8601.")

#Garante que o campo type da tabela transactions receba um id válido (1=INCOME, 2=EXPENSES, 3=TRANSFER
def _resolve_type_id(cur, type_id: Optional[int], type_name: Optional[str]) -> Optional[int]:
    if type_name:
        t = type_name.strip().upper()
        if t in TYPE_ALIASES:
            t = TYPE_ALIASES[t]
        cur.execute("SELECT id FROM transaction_types WHERE UPPER(type)=%s LIMIT 1;", (t,))
        row = cur.fetchone()
        return row[0] if row else None
    if type_id:
        return int(type_id)
    return 2

# Garante que o campo category_id da tabela transactions receba um id válido
# A função resolve a categoria a partir do nome (usando CATEGORY_ALIASES)
# ou utiliza diretamente o id informado.
# Caso nenhum valor seja passado, retorna uma categoria padrão (ex: 12 = "outros").
def _resolve_category_id(cur, category_id: Optional[int], category_name: Optional[str]) -> Optional[int]:
    if category_name:
        category = category_name.strip().upper()
        cur.execute("SELECT id FROM categories WHERE UPPER(name)=%s LIMIT 1;", (category,))
        row = cur.fetchone()
        return row[0] if row else None
    if category_id:
        return int(category_id)
    return 12

def _local_date_filter_sql(field: str = "occurred_at") -> str:
    """
    Retorna um trecho SQL para filtragem por dia local em America/Sao_Paulo.
    Ex.: (occurred_at AT TIME ZONE 'America/Sao_Paulo')::date = %s::date
    """
    return f"(({field} AT TIME ZONE 'America/Sao_Paulo')::date = %s::date)"

# Tool: add_transaction
@tool("add_transaction", args_schema=AddTransactionArgs)
def add_transaction(
    amount: float,
    description: str,
    source_text: str,
    occurred_at: Optional[str] = None,
    type_id: Optional[int] = None,
    type_name: Optional[str] = None,
    category_id: Optional[int] = None,
    category_name: Optional[str] = None,
    payment_method: Optional[str] = None,
) -> dict:
    """Insere uma transação financeira no banco de dados Postgres.""" # docstring obrigatório da @tools do langchain (estranho, mas legal né?)
    conn = get_conn()
    cur = conn.cursor()
    try:
        resolved_type_id = _resolve_type_id(cur, type_id, type_name)
        if not resolved_type_id:
            return {"status": "error", "message": "Tipo inválido (use type_id ou type_name: INCOME/EXPENSES/TRANSFER)."}

        resolved_category_id = _resolve_category_id(cur, category_id, category_name)
        if not resolved_category_id:
            return {
                "status": "error",
                "message": "Categoria inválida (use category_id ou category_name: comida, transporte, etc)."
            }

        if occurred_at:
            if len(occurred_at.strip()) == 10:
                occurred_at = f"{occurred_at}T12:00:00-03:00"
        
            cur.execute(
                """
                INSERT INTO transactions
                    (amount, type, category_id, description, payment_method, occurred_at, source_text)
                VALUES
                    (%s, %s, %s, %s, %s, %s::timestamptz, %s)
                RETURNING id, occurred_at;
                """,
                (amount, resolved_type_id, resolved_category_id, description, payment_method, occurred_at, source_text),
            )
        else:
            cur.execute(
                """
                INSERT INTO transactions
                    (amount, type, category_id, description, payment_method, occurred_at, source_text)
                VALUES
                    (%s, %s, %s, %s, %s, NOW(), %s)
                RETURNING id, occurred_at;
                """,
                (amount, resolved_type_id, resolved_category_id, description, payment_method, source_text),
            )

        new_id, occurred = cur.fetchone()
        conn.commit()
        return {"status": "ok", "id": new_id, "occurred_at": str(occurred)}

    except Exception as e:
        conn.rollback()
        return {"status": "error", "message": str(e)}
    finally:
        try:
            cur.close()
            conn.close()
        except Exception:
            pass
        
@tool("search_transactions", args_schema=SearchTransactionArgs)
def search_transactions(
    text: Optional[str] = None,
    type_id: Optional[int] = None,
    type_name: Optional[str] = None,
    category_id: Optional[int] = None,
    category_name: Optional[str] = None,
    init_date: Optional[str] = None,
    end_date: Optional[str] = None,
    payment_method: Optional[str] = None,
) -> dict:
    """
    Consulta todas as transações que ocorreram a partir de uma filtragem por texto, tipo e datas (em São Paulo).
    Os dados virão na seguinte ordem:
    - Caso seja filtrado um intervalo de data: ASC (mais antigos primeiros)
    - Caso contrário: DESC (mais recentes primeiro)
    """
    conn = get_conn()
    cur = conn.cursor()
    where = []
    params = []
    try:
        resolved_type_id = type_id
        if type_id == None and type_name != None:
            resolved_type_id = _resolve_type_id(cur, type_id, type_name)
    
        if resolved_type_id != None:
            where.append("AND t.type = %s")
            params.append(resolved_type_id)
            
        resolved_category_id = category_id
        if category_id is None and category_name is not None:
            resolved_category_id = _resolve_category_id(cur, category_id, category_name)

        if resolved_category_id is not None:
            where.append("AND t.category_id = %s")
            params.append(resolved_category_id)
            
        if text:
            where.append("AND t.description ILIKE %s")
            params.append(text)
            
        if init_date:
            where.append("AND (t.occurred_at AT TIME ZONE 'America/Sao_Paulo')::date >= %s::date")
            params.append(init_date)
            
        if end_date:
            where.append("AND (t.occurred_at AT TIME ZONE 'America/Sao_Paulo')::date <= %s::date")
            params.append(end_date)
            
        if payment_method:
            where.append("AND payment_method ILIKE %s")
            params.append(payment_method)
            
        order = "ASC" if (init_date or end_date) else "DESC"
        where_clause = " ".join(where)
        
        cur.execute(f"""
            SELECT 
                t.amount, 
                c.name, 
                t.description, 
                t.source_text, 
                (t.occurred_at AT TIME ZONE 'America/Sao_Paulo') AS occurred_at,
                tt.type 
            FROM transactions t 
            JOIN transaction_types tt 
                ON tt.id = t.type 
            JOIN categories c 
                ON c.id = t.category_id
            WHERE 1 = 1 {where_clause} 
            ORDER BY  t.occurred_at {order}
        """, params)
        
        rows = cur.fetchall()
        print('rows: ',rows)
        return {"status": "ok", "data": rows}
    except Exception as e:    
        return {"status": "error", "message": str(e)}
    finally:
        cur.close()
        conn.close()

@tool("saldo_total")
def saldo_total() -> dict:
    """
    Retorna o saldo total levando em conta apenas as entradas (INCOME)
    e as saídas (EXPENSES) ao longo de todo o histórico, 
    desconsiderando as transferências (TRANSFER).
    """
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT
                COALESCE(SUM(amount) FILTER (WHERE type = 1), 0) AS total_income,
                COALESCE(SUM(amount) FILTER (WHERE type = 2), 0) AS total_expenses
            FROM transactions;          
        """)
        
        income, expenses = cur.fetchone()
        return {
            "status": "ok",
            "saldo": float(income - expenses)
        }
    except Exception as e:    
        return {"status": "error", "message": str(e)}
    finally:
        cur.close()
        conn.close()
        
@tool("saldo_diario", args_schema=SaldoDiarioArgs)
def saldo_diario(date_local: str) -> dict:
    """
    Retorna o saldo (INCOME - EXPENSES) do dia informado (YYYY-MM-DD) em São Paulo.
    Não considera as transferências (TRANSFER)
    """
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT
                COALESCE(SUM(amount) FILTER (WHERE type = 1 AND occurred_at::date <= %s::date), 0) AS total_income,
                COALESCE(SUM(amount) FILTER (WHERE type = 2 AND occurred_at::date <= %s::date), 0) AS total_expenses
            FROM transactions;       
        """,
        (date_local, date_local))
        
        income, expenses = cur.fetchone()
        return {
            "status": "ok",
            "saldo": float(income - expenses)
        }
    except Exception as e:    
        return {"status": "error", "message": str(e)}
    finally:
        cur.close()
        conn.close()
    
@tool("update_transaction", args_schema=UpdateTransactionArgs)
def update_transaction(
    id: Optional[int] = None,
    match_text: Optional[str] = None,
    date_local: Optional[str] = None,
    amount: Optional[float] = None,
    type_id: Optional[int] = None,
    type_name: Optional[str] = None,
    category_id: Optional[int] = None,
    category_name: Optional[str] = None,
    description: Optional[str] = None,
    payment_method: Optional[str] = None,
    occurred_at: Optional[str] = None,
) -> dict:
    """
    Atualiza uma transação existente.
    Estratégias:
      - Se 'id' for informado: atualiza diretamente por ID.
      - Caso contrário: localiza a transação mais recente que combine (match_text em source_text/description)
        E (date_local em America/Sao_Paulo), então atualiza.
    Retorna: status, rows_affected, id, e o registro atualizado.
    """
    if not any([amount, type_id, type_name, category_id, category_name, description, payment_method, occurred_at]):
        return {"status": "error", "message": "Nada para atualizar: forneça pelo menos um campo (amount, type, category, description, payment_method, occurred_at)."}

    conn = get_conn()
    cur = conn.cursor()
    try:
        # Resolve target_id
        target_id = id
        if target_id is None:
            if not match_text or not date_local:
                return {"status": "error", "message": "Sem 'id': informe match_text E date_local para localizar o registro."}

            # Buscar o mais recente no dia local informado que combine o texto
            cur.execute(
                f"""
                SELECT t.id
                FROM transactions t
                WHERE (t.source_text ILIKE %s OR t.description ILIKE %s)
                  AND {_local_date_filter_sql("t.occurred_at")}
                ORDER BY t.occurred_at DESC
                LIMIT 1;
                """,
                (f"%{match_text}%", f"%{match_text}%", date_local)
            )
            row = cur.fetchone()
            if not row:
                return {"status": "error", "message": "Nenhuma transação encontrada para os filtros fornecidos."}
            target_id = row[0]

        # Resolver type_id / category_id a partir de nomes, se fornecidos
        resolved_type_id = _resolve_type_id(cur, type_id, type_name) if (type_id or type_name) else None
        resolved_category_id = category_id
        if category_name and not category_id:
            resolved_category_id = resolved_category_id(cur, category_name)

        # Montar SET dinâmico
        sets = []
        params: List[object] = []
        if amount is not None:
            sets.append("amount = %s")
            params.append(amount)
        if resolved_type_id is not None:
            sets.append("type = %s")
            params.append(resolved_type_id)
        if resolved_category_id is not None:
            sets.append("category_id = %s")
            params.append(resolved_category_id)
        if description is not None:
            sets.append("description = %s")
            params.append(description)
        if payment_method is not None:
            sets.append("payment_method = %s")
            params.append(payment_method)
        if occurred_at is not None:
            sets.append("occurred_at = %s::timestamptz")
            params.append(occurred_at)

        if not sets:
            return {"status": "error", "message": "Nenhum campo válido para atualizar."}

        params.append(target_id)

        cur.execute(
            f"UPDATE transactions SET {', '.join(sets)} WHERE id = %s;",
            params
        )
        rows_affected = cur.rowcount
        conn.commit()

        # Retornar o registro atualizado
        cur.execute(
            """
            SELECT
              t.id, t.occurred_at, t.amount, tt.type AS type_name,
              c.name AS category_name, t.description, t.payment_method, t.source_text
            FROM transactions t
            JOIN transaction_types tt ON tt.id = t.type
            LEFT JOIN categories c ON c.id = t.category_id
            WHERE t.id = %s;
            """,
            (target_id,)
        )
        r = cur.fetchone()
        updated = None
        if r:
            updated = {
                "id": r[0],
                "occurred_at": str(r[1]),
                "amount": float(r[2]),
                "type": r[3],
                "category": r[4],
                "description": r[5],
                "payment_method": r[6],
                "source_text": r[7],
            }

        return {
            "status": "ok",
            "rows_affected": rows_affected,
            "id": target_id,
            "updated": updated
        }

    except Exception as e:
        conn.rollback()
        return {"status": "error", "message": str(e)}
    finally:
        try:
            cur.close()
            conn.close()
        except Exception:
            pass


# Exporta a lista de tools
TOOLS = [add_transaction, saldo_total, saldo_diario, search_transactions, update_transaction]