"""Build the business operation catalog from configured read-only metadata endpoints."""
from copy import deepcopy


def operations(templates):
    catalog={}
    readonly_tools={"airflow_run_states":("mwaa-airflow","list_dag_runs"),
                    "clickhouse_columns":("clickhouse-prod","describe_table"),
                    "clickhouse_hourly_snapshots":("clickhouse-prod","query")}
    for name,original in templates.items():
        expected=readonly_tools.get(name,("redshift-mcp-server","list_columns"))
        if (original.get("server"),original.get("tool"))!=expected:
            continue
        if name in {"airflow_run_states","clickhouse_columns","clickhouse_hourly_snapshots"} or (
            name.startswith("redshift_") and name.endswith("_columns")
        ):
            catalog[name]=deepcopy(original)
            if name=="clickhouse_hourly_snapshots":
                catalog[name]["args"]={"sql":"SELECT count() AS n FROM system.tables WHERE database='moego_reporting' AND startsWith(name, concat('{table}', '_')) AND match(right(name,15),'^[0-9]{{8}}T[0-9]{{6}}$')","max_rows":1}
            catalog[name]["purpose"]=original["purpose"].replace("PR 引用","本次排查涉及").replace("PR 中","请求中")
        if not (name.startswith("redshift_") and name.endswith("_columns")):
            continue
        prefix=name.removesuffix("_columns")
        args=original["args"]
        catalog[prefix+"_aggregate_sql"]={
            "kind":"aggregate_sql","display_name":"Redshift","server":original["server"],"tool":"execute_query",
            "server_env":{"REDSHIFT_ALLOW_WRITE":"false"},"cluster":args["cluster_identifier"],"database":args["column_database_name"],
            "schemas":original["choices"]["schema"],
            "params":{"sql":"Redshift SELECT","start":"inclusive YYYY-MM-DD","end":"inclusive YYYY-MM-DD"},
            "purpose":args["column_database_name"]+" 多表汇总：自行编写 SELECT，可用 CTE、显式等值 JOIN ON、CASE、COUNT、SUM、AVG。每个物理表必须写 schema.table 并来自线程或已取得的证据；每个物理扫描自动限制 pt 至 start/end（31 天内），小时条件自行写。最终只输出 AS 命名的数值汇总及可选 CAST(col AS DATE) 日期。不要 SELECT 明细、星号或任意函数。最多 31 行。"}
        table='"{schema}"."{table}"'
        where='"{date_column}" >= \'{start}\' AND "{date_column}" < \'{until}\''
        day='CAST("{date_column}" AS DATE)'
        for suffix,label,measure,extra,hourly in [
            ("daily_count","逐日行数","COUNT(*)",{},False),
            ("daily_missing","逐日总行数与字段 NULL 数","COUNT(*), SUM(CASE WHEN \"{column}\" IS NULL THEN 1 ELSE 0 END)",{"column":"identifier"},False),
            ("daily_distinct","逐日总行数与字段去重数","COUNT(*), COUNT(DISTINCT \"{column}\")",{"column":"identifier"},False),
            ("daily_sum","逐日行数与指定数值列合计","COUNT(*), SUM(CAST(\"{column}\" AS DECIMAL(38,6)))",{"column":"identifier"},False),
            ("daily_latest","逐日行数与指定时间戳列最大值","COUNT(*), MAX(CAST(\"{column}\" AS TIMESTAMP))",{"column":"identifier"},False),
            ("hour_count","指定 p_hour 的逐日行数","COUNT(*)",{"hour":"hour"},True),
            ("hour_missing","指定 p_hour 的逐日总行数与字段 NULL 数","COUNT(*), SUM(CASE WHEN \"{column}\" IS NULL THEN 1 ELSE 0 END)",{"hour":"hour","column":"identifier"},True),
        ]:
            predicate=where+(' AND p_hour = {hour}' if hourly else '')
            sql=f'SELECT {day} AS day, {measure} FROM {table} WHERE {predicate} GROUP BY 1 ORDER BY 1 LIMIT 31'
            fields={"date":[0],"rows":[1]}
            if "missing" in suffix: fields["null_rows"]=[2]
            if "distinct" in suffix: fields["distinct_values"]=[2]
            if suffix=="daily_sum": fields["sum"]=[2]
            if suffix=="daily_latest": fields["latest_timestamp"]=[2]
            catalog[prefix+"_"+suffix]={
                "purpose":args["column_database_name"]+" / "+label+"；start、end 为含首尾的日期，最多 31 天，默认分区列 pt。",
                "server":original["server"],"tool":"execute_query","server_env":{"REDSHIFT_ALLOW_WRITE":"false"},
                "params":{"schema":"identifier","table":"identifier","date_column":"identifier","start":"date","end":"date",**extra},
                "args":{"cluster_identifier":args["cluster_identifier"],"database_name":args["column_database_name"],"sql":sql},
                "choices":original["choices"],"defaults":{"date_column":["pt"]},"fields":fields,"path":["rows"],"display_name":"Redshift"}
        catalog[prefix+"_find_tables"]={
            "purpose":"在 "+args["column_database_name"]+" 的指定 schema 按表名片段查找最多 12 张表；keyword 必须来自请求或工单。",
            "server":original["server"],"tool":"execute_query","server_env":{"REDSHIFT_ALLOW_WRITE":"false"},
            "params":{"schema":"identifier","keyword":"identifier"},
            "args":{"cluster_identifier":args["cluster_identifier"],"database_name":args["column_database_name"],
                    "sql":"SELECT table_schema, table_name FROM information_schema.tables WHERE table_schema='{schema}' AND POSITION(LOWER('{keyword}') IN LOWER(table_name)) > 0 ORDER BY table_name LIMIT 12"},
            "choices":original["choices"],"fields":{"schema":[0],"table":[1]},"path":["rows"],"display_name":"Redshift"}
    catalog["jira_issue"]={"kind":"jira","purpose":"按工单号读取标题、状态与限定长度的描述，不读取评论或附件。",
        "server":"mcp-atlassian","tool":"jira_get_issue","params":{"issue":"issue"},
        "args":{"issue_key":"{issue}","fields":"summary,status,description","comment_limit":0,"update_history":False},"display_name":"Jira"}
    catalog["jira_find"]={"kind":"jira_search","purpose":"在已登记业务项目中按请求中的主题词查找最多五张工单。",
        "server":"mcp-atlassian","tool":"jira_search","params":{"phrase":"phrase"},
        "args":{"jql":'project IN ({projects}) AND summary ~ "{phrase}" ORDER BY updated DESC',"fields":"summary,status","limit":5},"display_name":"Jira"}
    return catalog
