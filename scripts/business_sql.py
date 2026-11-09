"""Validate an analyst-authored Redshift SELECT before sending canonical SQL to MCP."""
import hashlib
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope

from review_evidence import Unavailable

IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,126}\Z", re.ASCII)
NODES = set("""
select from with cte subquery union join table tablealias identifier column alias distinct
where group having qualify order ordered limit star literal boolean null paren
and or not eq neq gt gte lt lte is in between like ilike
add sub mul div mod neg case if cast datatype datatypeparam
count sum avg min max coalesce nullif round abs filter
window rownumber partition
""".split())
SAFE_TYPES = {"DATE","TIMESTAMP","TIMESTAMPTZ","DATETIME","DECIMAL","BIGINT","INT","SMALLINT","DOUBLE","FLOAT","VARCHAR","TEXT","BOOLEAN"}
AGG = (exp.Count,exp.Sum,exp.Avg,exp.Min,exp.Max)


def deny(reason):
    raise Unavailable(reason)


def projection(expression):
    if isinstance(expression,exp.Paren):
        return projection(expression.this)
    if isinstance(expression,AGG):
        # MIN/MAX preserve their input type. Require an explicit numeric cast
        # so neither identifiers nor text can be returned as "aggregates".
        if isinstance(expression,(exp.Min,exp.Max)):
            value=expression.this
            if not isinstance(value,exp.Cast) or value.to.this.name not in {"DECIMAL","BIGINT","INT","DOUBLE","FLOAT","SMALLINT"}:
                deny("MIN/MAX 输出须显式转换为数值汇总")
        return "number"
    if isinstance(expression,(exp.Add,exp.Sub,exp.Mul,exp.Div)):
        kinds=[projection(expression.this),projection(expression.expression)]
        if all(k in ("number","constant") for k in kinds) and "number" in kinds:return "number"
    if isinstance(expression,exp.Literal) and not expression.is_string:return "constant"
    if isinstance(expression,exp.Cast) and expression.to.this.name=="DATE" and isinstance(expression.this,exp.Column):
        return "day"
    deny("最终输出只允许有别名的数值聚合、聚合算式和按日期分组，不返回明细")


def date_bounds(args):
    try:
        if not all(isinstance(args[k],str) and re.fullmatch(r"\d{4}-\d\d-\d\d",args[k]) for k in ("start","end")):
            raise ValueError
        start,end=(date.fromisoformat(args[k]) for k in ("start","end"))
        until=end+timedelta(days=1)
        if not 0<=(end-start).days<=30:raise ValueError
    except (ValueError,OverflowError,KeyError):deny("需提供含首尾、最多 31 天的有效日期范围")
    return start.isoformat(),until.isoformat()


def referenced(database,schema,name,source,history):
    # A reference qualified with another database does not authorize this one.
    pattern=r"(?<![\w.])(?:([a-z_][\w]*)\.)?"+re.escape(schema+"."+name)+r"(?![\w])"
    cleaned=source.replace('"',"").replace("`","")
    if any(not db or db.lower()==database for db in re.findall(pattern,cleaned,re.I|re.ASCII)):
        return True
    for item in history:
        if not item["result"].get("ok") or not item["name"].endswith("_find_tables"):continue
        if item.get("database")!=database:continue
        for row in item["result"]["data"]["records"]:
            if row.get("schema")==schema and row.get("table")==name:return True
    return False


def validate_join(join,prior):
    if join.args.get("kind") not in (None,"INNER","OUTER") or join.args.get("side") not in (None,"LEFT","RIGHT","FULL"):
        deny("不允许 CROSS、NATURAL 或特殊 JOIN")
    if join.args.get("method") or join.args.get("using"):deny("请用显式 ON 指定 JOIN 键")
    incoming=join.this.alias_or_name.lower()
    on=join.args.get("on")
    if on is None:deny("JOIN 必须有连接键")
    def terms(expr):
        if isinstance(expr,exp.Paren):return terms(expr.this)
        if isinstance(expr,exp.And):return terms(expr.this)+terms(expr.expression)
        return [expr]
    for term in terms(on):
        if isinstance(term,exp.EQ) and isinstance(term.this,exp.Column) and isinstance(term.expression,exp.Column):
            a,b=term.this.table.lower(),term.expression.table.lower()
            if (a==incoming and b in prior) or (b==incoming and a in prior):return
    deny("每个 JOIN 必须在 ON 中把新表的列与已有表的列直接等值关联")


def compile_sql(op,args,source,history):
    if sqlglot.__version__!="30.14.0":deny("SQL 校验器版本未经验证，未执行；需安装 requirements-business-sql.txt 中的版本")
    if not isinstance(args,dict) or set(args)!={"sql","start","end"}:deny("需提供 sql、start、end")
    start,until=date_bounds(args)
    sql=args["sql"]
    if not isinstance(sql,str) or len(sql)>10000:deny("SQL 为空或超过长度限制")
    sql=sql.strip().removesuffix(";")
    if any(x in sql for x in (";","--","/*","*/","\\","$","\x00")):deny("只允许一条不含注释或转义序列的 SELECT")
    try:
        parsed=sqlglot.parse(sql,read="redshift")
        if len(parsed)!=1 or not isinstance(parsed[0],exp.Select):deny("只允许 SELECT 或以 SELECT 结束的 WITH")
        root=parsed[0]
        nodes=list(root.walk())
        if len(nodes)>600 or any(n.key not in NODES for n in nodes):deny("语句包含未开放的表达式、函数或写入操作")
        if len(list(root.find_all(exp.Select)))>10 or len(list(root.find_all(exp.Join)))>6:deny("查询过于复杂，请拆分核验")
        for n in nodes:
            if isinstance(n,exp.Identifier) and not IDENT.fullmatch(n.name):deny("无效标识符")
            if isinstance(n,exp.DataType) and n.this.name not in SAFE_TYPES:deny("不支持该转换类型")
            if isinstance(n,exp.TableAlias) and n.args.get("columns"):deny("不支持表别名重命名列")
            if isinstance(n,exp.Star) and not isinstance(n.parent,exp.Count):deny("通配符只用于 COUNT(*)")
            if isinstance(n,exp.With) and n.args.get("recursive"):deny("不允许递归 CTE")
            if isinstance(n,exp.Column) and (n.db or n.catalog):deny("请使用表别名引用列")
            if isinstance(n,exp.Select):
                source_node=n.args.get("from_")
                prior={source_node.this.alias_or_name.lower()} if source_node else set()
                for join in n.args.get("joins",[]):
                    validate_join(join,prior)
                    prior.add(join.this.alias_or_name.lower())

        output={}
        for item in root.expressions:
            if not isinstance(item,exp.Alias):deny("每个输出指标或日期都需要 AS 别名")
            name=item.alias.lower()
            if name in output or len(output)>=12:deny("输出别名重复或超过 12 列")
            kind=projection(item.this)
            if kind=="constant":deny("不能输出单独的常量")
            output[name]=kind
        if "number" not in output.values():deny("必须输出聚合指标")
        if root.args.get("group"):
            for item in root.args["group"].expressions:
                if isinstance(item,exp.Literal) and str(item.this).isdigit() and 1<=int(item.this)<=len(output):
                    if list(output.values())[int(item.this)-1]=="day":continue
                if isinstance(item,exp.Column) and not item.table and output.get(item.name.lower())=="day":continue
                if projection(item)!="day":deny("最终结果仅支持按日期分组")

        database=op["database"].lower()
        physical=[]
        checked=set()
        for scope in traverse_scope(root):
            for _,(node,resolved) in scope.selected_sources.items():
                if not isinstance(node,exp.Table):continue
                checked.add(id(node))
                if not node.db and isinstance(resolved,Scope):continue
                schema,name=node.db.lower(),node.name.lower()
                if not schema or schema not in op["schemas"] or node.catalog and node.catalog.lower()!=database:
                    deny("物理表须属于所选数据库及已开放 schema；未限定表只允许是 CTE")
                if not referenced(database,schema,name,source,history):deny("该表未在当前问题、线程或已核实的工单/找表结果中指明")
                physical.append(node)
        if not physical or any(id(t) not in checked for t in root.find_all(exp.Table)):
            deny("无法确定全部数据来源")
        for node in physical:
            actual=node.copy();actual.set("alias",None)
            pt=exp.column("pt",quoted=True)
            bounded=exp.select("*").from_(actual).where(exp.GTE(this=pt.copy(),expression=exp.Literal.string(start)),exp.LT(this=pt.copy(),expression=exp.Literal.string(until)))
            node.replace(bounded.subquery(node.alias_or_name))
        # Fetch one sentinel row to distinguish exactly 31 groups from truncation.
        original_limit=root.args.get("limit")
        if original_limit is not None:
            limit=original_limit.expression
            if not isinstance(limit,exp.Literal) or limit.is_string or not str(limit.this).isdigit():deny("LIMIT 必须是非负整数")
            bound=min(32,int(limit.this))
        else:bound=32
        canonical=root.limit(bound).sql(dialect="redshift",unsupported_level=sqlglot.ErrorLevel.RAISE)
        return {"cluster_identifier":op["cluster"],"database_name":op["database"],"sql":canonical},output
    except Unavailable:raise
    except (sqlglot.errors.SqlglotError,AttributeError,KeyError,ValueError,TypeError,RecursionError):
        deny("无法安全解析这条 Redshift SELECT；请简化查询或检查别名")


def query_info(sql):
    parsed=sqlglot.parse_one(sql,read="redshift")
    return {"sha256":hashlib.sha256(sql.encode()).hexdigest(),"joins":len(list(parsed.find_all(exp.Join))),
            "tables":sorted({t.sql(dialect="redshift") for t in parsed.find_all(exp.Table) if t.db})}


def project_result(data,output):
    if data.get("columns")!=list(output) or not isinstance(data.get("rows"),list):deny("结果列与已核验的 SQL 不一致")
    records=[]
    for row in data["rows"][:31]:
        if not isinstance(row,(list,tuple)) or len(row)!=len(output):deny("聚合结果格式不正确")
        record={}
        for (name,kind),value in zip(output.items(),row):
            if value is not None:
                s=str(value)
                if len(s)>80:deny("结果超出汇总字段限制")
                try:
                    if kind=="number":
                        if not Decimal(s).is_finite():raise ValueError
                    elif not re.fullmatch(r"\d{4}-\d\d-\d\d",s):raise ValueError
                    else:date.fromisoformat(s)
                except (ValueError,InvalidOperation):deny("查询未返回数值或日期汇总")
            record[name]=value
        records.append(record)
    return {"records":records,"truncated":len(data["rows"])>31}
