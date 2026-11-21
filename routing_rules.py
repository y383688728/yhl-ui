import ipaddress
import re


DEFAULT_RULES = ["MATCH,节点"]
MAX_RULE_BYTES = 65536
BUILTIN_TARGETS = {"节点", "DIRECT", "REJECT", "REJECT-DROP", "PASS", "PASS-RULE", "COMPATIBLE"}
RULE_TYPES = set("""
    DOMAIN DOMAIN-SUFFIX DOMAIN-KEYWORD DOMAIN-REGEX DOMAIN-WILDCARD
    GEOSITE GEOIP SRC-GEOIP IP-ASN SRC-IP-ASN IP-CIDR IP-CIDR6 SRC-IP-CIDR
    IP-SUFFIX SRC-IP-SUFFIX SRC-PORT DST-PORT IN-PORT DSCP PROCESS-NAME PROCESS-PATH
    PROCESS-NAME-REGEX PROCESS-PATH-REGEX PROCESS-NAME-WILDCARD PROCESS-PATH-WILDCARD
    NETWORK UID IN-TYPE IN-USER IN-NAME REMATCH-NAME AND OR NOT MATCH
""".split())
COMMA_PAYLOAD_TYPES = {"AND", "OR", "NOT", "DOMAIN-REGEX", "PROCESS-NAME-REGEX", "PROCESS-PATH-REGEX"}


def normalize_rule(value):
    fields = [part.strip() for part in value.split(",")]
    kind = fields[0].upper()
    fields[0] = kind
    if kind in {"RULE-SET", "SUB-RULE"}:
        raise ValueError(f"{kind} 需要规则集或子规则定义，不能单独粘贴到这里。")
    if kind not in RULE_TYPES:
        raise ValueError(f"不支持的规则类型 {kind or '（空）'}。")
    if kind == "MATCH":
        if len(fields) != 2:
            raise ValueError("MATCH 格式为 MATCH,策略。")
        target_index = 1
    else:
        if len(fields) < 3 or not fields[1]:
            raise ValueError("规则缺少匹配条件或策略。")
        target_index = len(fields) - 1 if kind in COMMA_PAYLOAD_TYPES else 2
    target = fields[target_index]
    if target == "PROXY":
        target = "节点"
    if target not in BUILTIN_TARGETS:
        raise ValueError(f"策略「{target}」不存在，请改用 节点、PROXY、DIRECT 或 REJECT 等可用策略。")
    fields[target_index] = target
    if kind in {"AND", "OR", "NOT"}:
        payload = ",".join(fields[1:target_index])
        if re.search(r"\(\s*(RULE-SET|SUB-RULE)\s*,", payload, re.I):
            raise ValueError("组合规则引用了未定义的规则集或子规则。")
        depth = 0
        for char in payload:
            depth += (char == "(") - (char == ")")
            if depth < 0:
                break
        if depth or not payload.startswith("(") or not payload.endswith(")"):
            raise ValueError("组合规则的括号不成对。")
    if kind in {"IP-CIDR", "IP-CIDR6", "SRC-IP-CIDR"}:
        try:
            ipaddress.ip_network(fields[1], strict=False)
        except ValueError as error:
            raise ValueError("IP 网段格式无效。") from error
    return ",".join(fields)


def parse_rules(source):
    from ruamel.yaml import YAML
    from ruamel.yaml.error import YAMLError
    from ruamel.yaml.events import AliasEvent, MappingEndEvent, MappingStartEvent, SequenceEndEvent, SequenceStartEvent

    if len(source.encode("utf-8")) > MAX_RULE_BYTES:
        raise ValueError("规则文本超过 64 KiB。")
    if not source.strip():
        return None
    yaml = YAML(typ="safe", pure=True)
    try:
        depth = 0
        for event in yaml.parse(source):
            if isinstance(event, AliasEvent):
                raise ValueError("规则区不支持 YAML 别名。")
            if isinstance(event, (MappingStartEvent, SequenceStartEvent)):
                depth += 1
                if depth > 2:
                    raise ValueError("请粘贴 rules: 及其字符串列表。")
            elif isinstance(event, (MappingEndEvent, SequenceEndEvent)):
                depth -= 1
        data = YAML(typ="safe", pure=True).load(source)
    except YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        context = f"第 {mark.line + 1} 行附近" if mark is not None else "输入内容"
        raise ValueError(f"{context}的 YAML 格式无效。") from error
    if isinstance(data, dict) and set(data) == {"rules"}:
        data = data["rules"]
    if not isinstance(data, list):
        raise ValueError("请粘贴 rules: 及其规则列表，或直接粘贴 YAML 规则列表。")
    if not data:
        return None
    normalized = []
    for position, value in enumerate(data, 1):
        if not isinstance(value, str) or not value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError(f"第 {position} 条规则须为单行文本。")
        try:
            normalized.append(normalize_rule(value))
        except ValueError as error:
            raise ValueError(f"第 {position} 条规则：{error}") from error
    if not any(rule.startswith("MATCH,") for rule in normalized):
        normalized.extend(DEFAULT_RULES)
    return {"source": source, "rules": normalized}
