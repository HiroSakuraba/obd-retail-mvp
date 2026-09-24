package com.glovebox.obd.diagnosis

/**
 * Local diagnostic engine: loads one graph JSON (see diagnostic-graphs/),
 * applies likelihood multipliers from completed tests, and returns ranked
 * hypotheses with a coarse confidence band.
 *
 * posterior(h) ∝ prior(h) × Π multipliers — no ML model, no network needed.
 * Includes a tiny dependency-free JSON parser and a one-line boolean
 * expression evaluator for the graphs' "when" rules.
 */

// ------------------------------------------------------------------ JSON --

sealed interface Json {
    data class Obj(val map: Map<String, Json>) : Json
    data class Arr(val items: List<Json>) : Json
    data class Str(val value: String) : Json
    data class Num(val value: Double) : Json
    data class Bool(val value: Boolean) : Json
    object Null : Json
}

private class JsonParser(private val s: String) {
    private var i = 0

    fun parse(): Json {
        skipWs()
        val v = parseValue()
        skipWs()
        require(i == s.length) { "trailing data at $i" }
        return v
    }

    private fun skipWs() {
        while (i < s.length && s[i].isWhitespace()) i++
    }

    private fun parseValue(): Json {
        skipWs()
        return when (s[i]) {
            '{' -> parseObj()
            '[' -> parseArr()
            '"' -> Json.Str(parseStr())
            't' -> { expect("true"); Json.Bool(true) }
            'f' -> { expect("false"); Json.Bool(false) }
            'n' -> { expect("null"); Json.Null }
            else -> parseNum()
        }
    }

    private fun expect(word: String) {
        require(s.startsWith(word, i)) { "expected $word at $i" }
        i += word.length
    }

    private fun parseObj(): Json.Obj {
        i++ // {
        val m = mutableMapOf<String, Json>()
        skipWs()
        if (s[i] == '}') { i++; return Json.Obj(m) }
        while (true) {
            skipWs()
            val k = parseStr()
            skipWs(); require(s[i] == ':') { "expected : at $i" }; i++
            m[k] = parseValue()
            skipWs()
            when (s[i]) {
                ',' -> i++
                '}' -> { i++; return Json.Obj(m) }
                else -> error("expected , or } at $i")
            }
        }
    }

    private fun parseArr(): Json.Arr {
        i++ // [
        val items = mutableListOf<Json>()
        skipWs()
        if (s[i] == ']') { i++; return Json.Arr(items) }
        while (true) {
            items += parseValue()
            skipWs()
            when (s[i]) {
                ',' -> i++
                ']' -> { i++; return Json.Arr(items) }
                else -> error("expected , or ] at $i")
            }
        }
    }

    private fun parseStr(): String {
        require(s[i] == '"'); i++
        val sb = StringBuilder()
        while (i < s.length && s[i] != '"') {
            if (s[i] == '\\') {
                i++
                sb.append(
                    when (s[i]) {
                        '"', '\\', '/' -> s[i]
                        'n' -> '\n'; 't' -> '\t'; 'r' -> '\r'
                        'u' -> { val h = s.substring(i + 1, i + 5); i += 4
                                h.toInt(16).toChar() }
                        else -> error("bad escape at $i")
                    }
                )
            } else sb.append(s[i])
            i++
        }
        require(i < s.length); i++ // closing "
        return sb.toString()
    }

    private fun parseNum(): Json.Num {
        val start = i
        while (i < s.length && s[i] in "-+0123456789.eE") i++
        return Json.Num(s.substring(start, i).toDouble())
    }
}

private fun Json.reqObj(key: String): Json.Obj =
    (this as Json.Obj).map[key] as? Json.Obj ?: error("missing object $key")
private fun Json.reqArr(key: String): List<Json> =
    ((this as Json.Obj).map[key] as? Json.Arr ?: error("missing array $key")).items
private fun Json.optStr(key: String): String? =
    ((this as Json.Obj).map[key] as? Json.Str)?.value
private fun Json.reqStr(key: String): String =
    optStr(key) ?: error("missing string $key")

// ------------------------------------------------------- "when" evaluator --

/** Evaluates one-line rules like "trim_idle - trim_2500 >= 8 and x != 0". */
private class RuleEvaluator(private val fields: Map<String, Double>) {
    private lateinit var toks: List<String>
    private var i = 0

    fun eval(expr: String): Boolean {
        toks = tokenize(expr)
        i = 0
        val v = parseOr()
        require(i == toks.size) { "trailing tokens in '$expr'" }
        return v != 0.0
    }

    private fun tokenize(e: String): List<String> {
        val out = mutableListOf<String>()
        var j = 0
        while (j < e.length) {
            val c = e[j]
            when {
                c.isWhitespace() -> j++
                c.isLetter() || c == '_' -> {
                    var k = j
                    while (k < e.length && (e[k].isLetterOrDigit() || e[k] == '_')) k++
                    out += e.substring(j, k); j = k
                }
                c.isDigit() || c == '.' -> {
                    var k = j
                    while (k < e.length && (e[k].isDigit() || e[k] == '.')) k++
                    out += e.substring(j, k); j = k
                }
                e.startsWith(">=", j) || e.startsWith("<=", j) ||
                    e.startsWith("==", j) || e.startsWith("!=", j) -> {
                    out += e.substring(j, j + 2); j += 2
                }
                c in "><+-*/()" -> { out += c.toString(); j++ }
                else -> error("bad char '$c' in rule")
            }
        }
        return out
    }

    private fun peek() = toks.getOrNull(i)
    private fun next() = toks[i++]

    private fun parseOr(): Double {
        var v = parseAnd()
        while (peek() == "or") { next(); v = if (v != 0.0 || parseAnd() != 0.0) 1.0 else 0.0 }
        return v
    }

    private fun parseAnd(): Double {
        var v = parseCmp()
        while (peek() == "and") { next(); v = if (v != 0.0 && parseCmp() != 0.0) 1.0 else 0.0 }
        return v
    }

    private fun parseCmp(): Double {
        if (peek() == "not") { next(); return if (parseCmp() == 0.0) 1.0 else 0.0 }
        val l = parseAdd()
        return when (peek()) {
            ">", ">=", "<", "<=", "==", "!=" -> {
                val op = next(); val r = parseAdd()
                val ok = when (op) {
                    ">" -> l > r; ">=" -> l >= r; "<" -> l < r
                    "<=" -> l <= r; "==" -> l == r; else -> l != r
                }
                if (ok) 1.0 else 0.0
            }
            else -> l
        }
    }

    private fun parseAdd(): Double {
        var v = parseMul()
        while (peek() == "+" || peek() == "-") {
            val op = next(); val r = parseMul()
            v = if (op == "+") v + r else v - r
        }
        return v
    }

    private fun parseMul(): Double {
        var v = parseAtom()
        while (peek() == "*" || peek() == "/") {
            val op = next(); val r = parseAtom()
            v = if (op == "*") v * r else v / r
        }
        return v
    }

    private fun parseAtom(): Double {
        val t = next()
        return when {
            t == "(" -> { val v = parseOr(); require(next() == ")"); v }
            t == "-" -> -parseAtom()
            t[0].isLetter() || t[0] == '_' ->
                fields[t] ?: error("unknown field '$t' in rule")
            else -> t.toDouble()
        }
    }
}

// ---------------------------------------------------------------- engine --

data class HypothesisScore(val id: String, val label: String, val posterior: Double)

enum class ConfidenceBand { STRONG, MODERATE, INSUFFICIENT }

data class DiagnosisResult(
    val hypotheses: List<HypothesisScore>,
    val top: HypothesisScore?,
    val band: ConfidenceBand,
    val stopMessage: String? = null
)

/**
 * @param graphJson one diagnostic-graphs JSON document.
 * @param observations testId -> field map. Guided tests use field "answer"
 *   with value 1.0 for "yes", 0.0 for "no".
 * @param flags safety observation flags, e.g. setOf("flashing_mil").
 */
class DiagnosticEngine(graphJson: String) {

    private val graph: Json.Obj = JsonParser(graphJson).parse() as Json.Obj
    private val stopIf: Set<String> =
        graph.reqObj("safety").reqArr("stop_if").map { (it as Json.Str).value }.toSet()
    private val stopMessage: String =
        graph.reqObj("safety").optStr("stop_message") ?: "Stop driving."
    private val testIds: List<String> =
        graph.reqArr("tests").map { (it as Json.Obj).reqStr("id") }

    /** First test (cheapest-first order) that has no observation yet. */
    fun nextTest(observedTestIds: Set<String>): String? =
        testIds.firstOrNull { it !in observedTestIds }

    fun score(
        observations: Map<String, Map<String, Double>>,
        flags: Set<String> = emptySet()
    ): DiagnosisResult {
        val hit = flags.firstOrNull { it in stopIf }
        if (hit != null) {
            return DiagnosisResult(emptyList(), null, ConfidenceBand.INSUFFICIENT, stopMessage)
        }

        val scores = mutableMapOf<String, Double>()
        val labels = mutableMapOf<String, String>()
        for (h in graph.reqArr("initial_hypotheses")) {
            h as Json.Obj
            val id = h.reqStr("id")
            scores[id] = (h.map["prior"] as Json.Num).value
            labels[id] = h.optStr("label") ?: id
        }

        for (t in graph.reqArr("tests")) {
            t as Json.Obj
            val tid = t.reqStr("id")
            val obs = observations[tid] ?: continue
            when (t.reqStr("kind")) {
                "obd_live" -> {
                    val ev = RuleEvaluator(obs)
                    for (rule in t.reqArr("result_rules")) {
                        rule as Json.Obj
                        val whenExpr = rule.reqStr("when")
                        if (runCatching { ev.eval(whenExpr) }.getOrDefault(false)) {
                            applyUpdates(scores, (rule.map["updates"] as Json.Obj).map)
                        }
                    }
                }
                "guided_visual", "guided_manual" -> {
                    if ((obs["answer"] ?: 0.0) == 1.0) {
                        val u = t.map["updates_if_yes"] as? Json.Obj
                        if (u != null) applyUpdates(scores, u.map)
                    }
                }
            }
        }

        val total = scores.values.sum().takeIf { it > 0 } ?: 1.0
        val ranked = scores.map { (id, w) ->
            HypothesisScore(id, labels[id] ?: id, w / total)
        }.sortedByDescending { it.posterior }
        val top = ranked.firstOrNull()
        val band = when {
            top == null -> ConfidenceBand.INSUFFICIENT
            top.posterior > 0.75 -> ConfidenceBand.STRONG
            top.posterior >= 0.45 -> ConfidenceBand.MODERATE
            else -> ConfidenceBand.INSUFFICIENT
        }
        return DiagnosisResult(ranked, top, band)
    }

    private fun applyUpdates(scores: MutableMap<String, Double>, updates: Map<String, Json>) {
        for ((hid, mult) in updates) {
            val m = (mult as Json.Num).value
            scores[hid] = (scores[hid] ?: 0.0) * m
        }
    }
}
