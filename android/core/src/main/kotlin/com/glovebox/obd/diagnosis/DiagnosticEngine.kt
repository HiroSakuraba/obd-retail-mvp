package com.glovebox.obd.diagnosis

import com.glovebox.obd.scan.DtcEvidence
import kotlin.math.abs
import kotlin.math.ln

/**
 * Local diagnostic engine: loads diagnostic-graphs JSON documents, applies
 * likelihood multipliers from completed tests, and returns ranked hypotheses
 * with a coarse confidence band.
 *
 * diagnostic_score(h) ∝ prior(h) × Π multipliers — no ML model, no network needed.
 * The scores are expert-authored weights, not calibrated probabilities:
 * "diagnostic_score" deliberately avoids claiming statistical calibration.
 *
 * Parity contract: this file mirrors backend/app/engine.py. The shared golden
 * vectors in tests/golden/diagnostic_vectors.json (generated from the backend
 * by tests/golden/gen_diagnostic_vectors.py) pin the exact behavior both
 * implementations must reproduce; GoldenVectorsTest.kt runs them here. If the
 * backend engine changes, regenerate the vectors and update this port together.
 *
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

/** Parse a JSON document; internal so the golden-vector test can reuse it. */
internal fun parseJsonDocument(text: String): Json = JsonParser(text).parse()

internal fun Json.reqObj(key: String): Json.Obj =
    (this as Json.Obj).map[key] as? Json.Obj ?: error("missing object $key")
internal fun Json.optObj(key: String): Json.Obj? =
    (this as Json.Obj).map[key] as? Json.Obj
internal fun Json.reqArr(key: String): List<Json> =
    ((this as Json.Obj).map[key] as? Json.Arr ?: error("missing array $key")).items
internal fun Json.optArr(key: String): List<Json>? =
    ((this as Json.Obj).map[key] as? Json.Arr)?.items
internal fun Json.optStr(key: String): String? =
    ((this as Json.Obj).map[key] as? Json.Str)?.value
internal fun Json.reqStr(key: String): String =
    optStr(key) ?: error("missing string $key")
internal fun Json.optNum(key: String): Double? =
    ((this as Json.Obj).map[key] as? Json.Num)?.value
internal fun Json.optBool(key: String): Boolean? =
    ((this as Json.Obj).map[key] as? Json.Bool)?.value
internal fun Json.reqBool(key: String): Boolean =
    optBool(key) ?: error("missing bool $key")
internal fun Json.strList(key: String): List<String> =
    optArr(key)?.map { (it as Json.Str).value } ?: emptyList()

/** Convert parsed JSON to plain Kotlin values (for recipe maps). */
internal fun Json.toAny(): Any? = when (this) {
    is Json.Obj -> map.mapValues { it.value.toAny() }
    is Json.Arr -> items.map { it.toAny() }
    is Json.Str -> value
    is Json.Num -> value
    is Json.Bool -> value
    is Json.Null -> null
}

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
        while (peek() == "or") {
            next()
            // Parse the right side first: Kotlin's || would skip it when the
            // left side is true, leaving its tokens unconsumed (regression:
            // "x != 0 or y != 0" with x=1 used to throw "trailing tokens").
            val rhs = parseAnd()
            v = if (v != 0.0 || rhs != 0.0) 1.0 else 0.0
        }
        return v
    }

    private fun parseAnd(): Double {
        var v = parseCmp()
        while (peek() == "and") {
            next()
            val rhs = parseCmp()
            v = if (v != 0.0 && rhs != 0.0) 1.0 else 0.0
        }
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
            // Python raises ZeroDivisionError on x/0 and the backend treats
            // the rule as not matched; mirror that instead of producing
            // Infinity/NaN.
            if (op == "/" && r == 0.0) throw ArithmeticException("division by zero in rule")
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

// ------------------------------------------------------------ graph model --

/**
 * One completed test. Guided tests carry a categorical [outcome]
 * ("leak_found", "stays_on_cylinder", …); obd_live tests carry measured
 * [fields]. The legacy flat shape (test_id -> field map, guided tests using
 * field "answer" = 1.0/0.0) is accepted by [fromJson].
 */
data class Observation(val fields: Map<String, Double>, val outcome: String? = null) {
    companion object {
        fun fromJson(value: Json): Observation {
            val obj = value as Json.Obj
            val fieldsJson = obj.map["fields"]
            return if (fieldsJson is Json.Obj) {
                Observation(
                    fieldsJson.map.mapValues { (it.value as Json.Num).value },
                    (obj.map["outcome"] as? Json.Str)?.value
                )
            } else {
                // Legacy flat shape: test_id -> {field: number}.
                Observation(obj.map.mapValues { (it.value as Json.Num).value }, null)
            }
        }
    }
}

data class HypothesisScore(val id: String, val label: String, val diagnostic_score: Double)

enum class ConfidenceBand { STRONG, MODERATE, INSUFFICIENT }

internal fun confidenceBand(p: Double): ConfidenceBand = when {
    p > 0.75 -> ConfidenceBand.STRONG
    p >= 0.45 -> ConfidenceBand.MODERATE
    else -> ConfidenceBand.INSUFFICIENT
}

/** One evidence_group dedup record: a weaker multiplier that was dropped. */
data class DedupedRecord(
    val testId: String,
    val evidenceGroup: String,
    val hypothesis: String,
    val droppedMultiplier: Double,
    val keptMultiplier: Double,
    val reason: String
)

/** One code_context entry that actually fired. */
data class CodeContextHit(
    val dtc: String,
    val hypothesis: String,
    val multiplier: Double,
    val why: String?
)

data class DiagnosisResult(
    val hypotheses: List<HypothesisScore>,
    val top: HypothesisScore?,
    val band: ConfidenceBand,
    val stopped: Boolean = false,
    val stopMessage: String? = null,
    val deduped: List<DedupedRecord> = emptyList(),
    val testOutcomes: Map<String, String?> = emptyMap(),
    val codeContextApplied: List<CodeContextHit> = emptyList()
)

data class ResultRule(
    val whenExpr: String,
    val updates: Map<String, Double>,
    val evidenceGroup: String?
)

data class GuidedOutcome(val label: String?, val updates: Map<String, Double>)

data class TestDef(
    val id: String,
    val kind: String,
    val question: String?,
    val resultRules: List<ResultRule>,
    val outcomes: Map<String, GuidedOutcome>,
    val updatesIfYes: Map<String, Double>,
    val signals: List<String>
)

data class CodeContextEntry(
    val dtc: String,
    val hypothesis: String,
    val multiplier: Double,
    val why: String?
)

data class RepairDef(
    val repairId: String,
    val hypothesis: String,
    val label: String,
    val action: String?,
    val component: String?,
    val recommendationClass: String,
    val minConfidence: String,
    val verification: String?,
    val tools: List<String>,
    val consumables: List<String>,
    val requiresAny: List<String>
)

private fun parseUpdates(obj: Json.Obj?): Map<String, Double> =
    obj?.map?.mapValues { (it.value as Json.Num).value } ?: emptyMap()

private fun parseTestDef(json: Json.Obj): TestDef {
    val outcomes = (json.optObj("outcomes")?.map ?: emptyMap()).mapValues { (_, v) ->
        val o = v as Json.Obj
        GuidedOutcome(o.optStr("label"), parseUpdates(o.optObj("updates")))
    }
    return TestDef(
        id = json.reqStr("id"),
        kind = json.reqStr("kind"),
        question = json.optStr("question"),
        resultRules = json.optArr("result_rules")?.mapIndexed { i, r ->
            r as Json.Obj
            ResultRule(
                whenExpr = r.reqStr("when"),
                updates = parseUpdates(r.reqObj("updates")),
                evidenceGroup = r.optStr("evidence_group") ?: "rule:$i"
            )
        } ?: emptyList(),
        outcomes = outcomes,
        updatesIfYes = parseUpdates(json.optObj("updates_if_yes")),
        signals = json.strList("signals")
    )
}

private fun parseCodeContextEntry(e: Json): CodeContextEntry {
    e as Json.Obj
    return CodeContextEntry(
        dtc = e.reqStr("dtc"),
        hypothesis = e.reqStr("hypothesis"),
        multiplier = e.optNum("multiplier") ?: 1.0,
        why = e.optStr("why")
    )
}

private fun parseRepairDef(r: Json): RepairDef {
    r as Json.Obj
    return RepairDef(
        repairId = r.reqStr("repair_id"),
        hypothesis = r.reqStr("hypothesis"),
        label = r.reqStr("label"),
        action = r.optStr("action"),
        component = r.optStr("component"),
        recommendationClass = r.reqStr("recommendation_class"),
        minConfidence = r.optStr("min_confidence") ?: "moderate",
        verification = r.optStr("verification"),
        tools = r.optArr("tools")?.map { ((it as Json.Obj).optStr("name") ?: "") } ?: emptyList(),
        consumables = r.strList("consumables"),
        requiresAny = r.strList("requires_any")
    )
}

/** Typed view over one diagnostic-graphs JSON document. */
class Graph internal constructor(internal val json: Json.Obj) {
    // "family" is required in diagnostic-graphs/ files; unit-test graphs may
    // omit it and get a placeholder.
    val family: String = json.optStr("family") ?: "graph"
    val title: String = json.optStr("title") ?: family
    val dtcs: Set<String> = json.strList("dtcs").toSet()
    val stopIf: Set<String> = json.optObj("safety")?.strList("stop_if")?.toSet() ?: emptySet()
    val stopMessage: String = json.optObj("safety")?.optStr("stop_message") ?: "Stop driving."
    val hypotheses: List<Pair<String, Pair<String, Double>>> =
        json.reqArr("initial_hypotheses").map { h ->
            h as Json.Obj
            val id = h.reqStr("id")
            id to ((h.optStr("label") ?: id) to (h.optNum("prior") ?: 0.0))
        }
    val tests: List<TestDef> = json.optArr("tests")?.map { parseTestDef(it as Json.Obj) } ?: emptyList()
    val testById: Map<String, TestDef> = tests.associateBy { it.id }
    val codeContext: List<CodeContextEntry> =
        (json.optObj("code_context")?.optArr("boost_if_present") ?: emptyList()).map(::parseCodeContextEntry) +
            (json.optObj("code_context")?.optArr("reduce_if_present") ?: emptyList()).map(::parseCodeContextEntry)
    val repairs: List<RepairDef> = json.optArr("repairs")?.map(::parseRepairDef) ?: emptyList()
    /** Declared measurement recipes, keyed by signal id, in graph order. */
    val signals: Map<String, Json.Obj> =
        (json.optArr("signals") ?: emptyList()).associate { s ->
            s as Json.Obj
            s.reqStr("signal") to s
        }
}

// ---------------------------------------------------------------- scoring --

/**
 * Evidence ids usable by requires_any: completed test ids plus
 * test_id:outcome for recorded categorical outcomes (mirrors the backend's
 * evidence_satisfied).
 */
internal fun evidenceSatisfied(observations: Map<String, Observation>): Set<String> {
    val sat = mutableSetOf<String>()
    for ((tid, obs) in observations) {
        sat += tid
        val outcome = obs.outcome
        if (outcome != null) {
            sat += "$tid:$outcome"
        } else if ((obs.fields["answer"] ?: 0.0) == 1.0) {
            sat += "$tid:yes"
        }
    }
    return sat
}

/**
 * Resolve a guided test's updates for the recorded outcome. Categorical
 * outcomes are preferred: a 'no' answer is evidence too. Falls back to the
 * legacy boolean updates_if_yes path.
 */
private fun guidedUpdates(
    test: TestDef,
    fields: Map<String, Double>,
    outcome: String?
): Pair<Map<String, Double>, String?> {
    val explicit = outcome?.let { test.outcomes[it] }
    if (explicit != null) return explicit.updates to outcome
    if ((outcome == null || outcome == "yes") && (fields["answer"] ?: 0.0) == 1.0) {
        if (outcome == "yes") {
            val yesOutcome = test.outcomes["yes"]
            if (yesOutcome != null) return yesOutcome.updates to "yes"
        }
        if (test.updatesIfYes.isNotEmpty()) return test.updatesIfYes to "yes"
    }
    return emptyMap<String, Double>() to outcome
}

internal fun scoreGraph(
    graph: Graph,
    observations: Map<String, Observation>,
    flags: Set<String> = emptySet(),
    dtcEvidence: List<DtcEvidence> = emptyList(),
    allDtcs: List<String>? = null
): DiagnosisResult {
    if (flags.any { it in graph.stopIf }) {
        return DiagnosisResult(emptyList(), null, ConfidenceBand.INSUFFICIENT,
            stopped = true, stopMessage = graph.stopMessage)
    }

    val scores = mutableMapOf<String, Double>()
    val labels = mutableMapOf<String, String>()
    for ((id, labelAndPrior) in graph.hypotheses) {
        scores[id] = labelAndPrior.second
        labels[id] = labelAndPrior.first
    }

    val deduped = mutableListOf<DedupedRecord>()
    val testOutcomes = mutableMapOf<String, String?>()

    for ((tid, obs) in observations) {
        val t = graph.testById[tid] ?: continue
        when (t.kind) {
            "obd_live" -> {
                // One measurement can match several rules. Rules sharing an
                // evidence_group are NOT independent experiments: per
                // hypothesis only the strongest |ln(multiplier)| applies.
                val groups = mutableMapOf<String, MutableMap<String, Pair<Double, Int>>>()
                val ev = RuleEvaluator(obs.fields)
                for ((i, rule) in t.resultRules.withIndex()) {
                    val matched = runCatching { ev.eval(rule.whenExpr) }
                        .getOrDefault(false)
                    if (!matched) continue
                    val gkey = rule.evidenceGroup ?: "rule:$i"
                    val bucket = groups.getOrPut(gkey) { mutableMapOf() }
                    for ((hid, m) in rule.updates) {
                        val prev = bucket[hid]
                        if (prev == null || abs(ln(m)) > abs(ln(prev.first))) {
                            if (prev != null) {
                                deduped += DedupedRecord(tid, gkey, hid, prev.first, m,
                                    "same measurement as a stronger rule")
                            }
                            bucket[hid] = m to i
                        } else {
                            deduped += DedupedRecord(tid, gkey, hid, m, prev.first,
                                "same measurement as a stronger rule")
                        }
                    }
                }
                for (bucket in groups.values) {
                    for ((hid, multAndRule) in bucket) {
                        scores[hid] = (scores[hid] ?: 0.0) * multAndRule.first
                    }
                }
            }
            "guided_visual", "guided_manual" -> {
                val (updates, resolved) = guidedUpdates(t, obs.fields, obs.outcome)
                testOutcomes[tid] = resolved
                for ((hid, m) in updates) {
                    scores[hid] = (scores[hid] ?: 0.0) * m
                }
            }
        }
    }

    // Companion codes reshape the hypotheses (P0171+P0174 is different
    // evidence from P0171 alone).
    val codeContextApplied = mutableListOf<CodeContextHit>()
    val codes = allDtcs?.toSet() ?: dtcEvidence.map { it.code }.toSet()
    for (entry in graph.codeContext) {
        if (entry.dtc in codes && entry.hypothesis in scores) {
            scores[entry.hypothesis] = scores.getValue(entry.hypothesis) * entry.multiplier
            codeContextApplied += CodeContextHit(entry.dtc, entry.hypothesis,
                entry.multiplier, entry.why)
        }
    }

    val total = scores.values.sum().takeIf { it > 0 } ?: 1.0
    val ranked = scores.map { (id, w) ->
        HypothesisScore(id, labels[id] ?: id, w / total)
    }.sortedByDescending { it.diagnostic_score }
    val top = ranked.firstOrNull()
    return DiagnosisResult(
        hypotheses = ranked,
        top = top,
        band = if (top != null) confidenceBand(top.diagnostic_score)
               else ConfidenceBand.INSUFFICIENT,
        deduped = deduped,
        testOutcomes = testOutcomes,
        codeContextApplied = codeContextApplied
    )
}

// ----------------------------------------------------------------- basket --

data class RepairView(
    val repairId: String,
    val hypothesis: String,
    val label: String,
    val action: String?,
    val component: String?,
    val recommendationClass: String,
    val minConfidence: String,
    val verification: String?,
    val tools: List<String>,
    val consumables: List<String>,
    val satisfiedEvidence: List<String> = emptyList()
)

data class GatedRepair(
    val repairId: String,
    val label: String,
    val missingEvidence: List<String>,
    val reason: String
)

data class RepairBasket(
    val confidence: ConfidenceBand,
    val topHypothesis: HypothesisScore?,
    val items: List<RepairView>,
    val guidance: List<RepairView>,
    val gated: List<GatedRepair>,
    val message: String
)

private fun bandRank(band: ConfidenceBand): Int = when (band) {
    ConfidenceBand.STRONG -> 2
    ConfidenceBand.MODERATE -> 1
    ConfidenceBand.INSUFFICIENT -> 0
}

private fun bandRank(name: String): Int = when (name) {
    "strong" -> 2
    "moderate" -> 1
    else -> 0
}

private fun repairView(r: RepairDef): RepairView = RepairView(
    repairId = r.repairId,
    hypothesis = r.hypothesis,
    label = r.label,
    action = r.action,
    component = r.component,
    recommendationClass = r.recommendationClass,
    minConfidence = r.minConfidence,
    verification = r.verification,
    tools = r.tools,
    consumables = r.consumables
)

/**
 * Basket for the top hypothesis, split by recommendation class.
 * Diagnostic-class entries are GUIDANCE — never a part sale.
 * Replacement-class entries are offered only when the score band is reached
 * AND requires_any evidence exists: a score alone never sells a part.
 */
internal fun buildRepairBasket(
    graph: Graph,
    scored: DiagnosisResult,
    observations: Map<String, Observation>
): RepairBasket {
    if (scored.stopped || scored.top == null || observations.isEmpty()) {
        return RepairBasket(ConfidenceBand.INSUFFICIENT, scored.top,
            emptyList(), emptyList(), emptyList(),
            "Not enough evidence yet — run the next test.")
    }
    val top = scored.top
    val band = scored.band
    val sat = evidenceSatisfied(observations)
    val items = mutableListOf<RepairView>()
    val guidance = mutableListOf<RepairView>()
    val gated = mutableListOf<GatedRepair>()
    for (r in graph.repairs) {
        if (r.hypothesis != top.id) continue
        if (bandRank(band) < bandRank(r.minConfidence)) continue
        if (r.recommendationClass == "diagnostic") {
            guidance += repairView(r)
            continue
        }
        val hit = r.requiresAny.filter { it in sat }
        if (hit.isNotEmpty()) {
            items += repairView(r).copy(satisfiedEvidence = hit)
        } else {
            gated += GatedRepair(r.repairId, r.label, r.requiresAny,
                "score band reached but the confirming evidence has not been recorded yet")
        }
    }
    val message = when {
        items.isNotEmpty() -> "Evidence supports this repair."
        guidance.isNotEmpty() -> "Next step: " + guidance.joinToString("; ") { it.label } +
            ". No part is recommended until the confirming evidence is recorded."
        else -> "Not enough evidence yet — run the next test."
    }
    return RepairBasket(band, top, items, guidance, gated, message)
}

// ---------------------------------------------------------------- engine --

/**
 * @param graphJson one diagnostic-graphs JSON document.
 */
class DiagnosticEngine(graphJson: String) {

    val graph: Graph = Graph(parseJsonDocument(graphJson) as Json.Obj)

    /** First test (cheapest-first order) that has no observation yet. */
    fun nextTest(observedTestIds: Set<String>): String? =
        graph.tests.firstOrNull { it.id !in observedTestIds }?.id

    /**
     * @param observations testId -> [Observation] (fields + optional
     *   categorical outcome).
     * @param flags safety observation flags, e.g. setOf("flashing_mil").
     * @param dtcEvidence codes carried from the scan (for code_context).
     * @param allDtcs every code on the vehicle; defaults to dtcEvidence codes.
     */
    fun score(
        observations: Map<String, Observation>,
        flags: Set<String> = emptySet(),
        dtcEvidence: List<DtcEvidence> = emptyList(),
        allDtcs: List<String>? = null
    ): DiagnosisResult = scoreGraph(graph, observations, flags, dtcEvidence, allDtcs)

    /**
     * Legacy overload: observations as testId -> field map. Guided tests use
     * field "answer" with value 1.0 for "yes", 0.0 for "no".
     */
    fun score(
        observations: Map<String, Map<String, Double>>,
        flags: Set<String> = emptySet()
    ): DiagnosisResult {
        val wrapped: Map<String, Observation> =
            observations.mapValues { Observation(it.value) }
        return scoreGraph(graph, wrapped, flags)
    }

    fun repairBasket(
        scored: DiagnosisResult,
        observations: Map<String, Observation>
    ): RepairBasket = buildRepairBasket(graph, scored, observations)
}

data class ScoredGraph(val graph: Graph, val result: DiagnosisResult)

data class NextTest(
    val graphFamily: String,
    val testId: String,
    val kind: String,
    val question: String?
)

/**
 * Multi-graph entry point: takes several graph JSON documents (usually the
 * result of [graphsForDtcs]) and scores them as a set, mirroring the
 * backend's graphs_for_dtcs / score_all / next_test / repair_basket.
 */
class GraphSet(graphJsons: List<String>) {

    val graphs: List<Graph> =
        graphJsons.map { Graph(parseJsonDocument(it) as Json.Obj) }

    /**
     * Every graph matching ANY of the codes, best match first: most matching
     * codes first, then family name. Real cars carry code clusters
     * (P0171+P0174, P0302+P0171); selecting one graph and discarding the rest
     * throws away diagnostic information.
     */
    fun graphsForDtcs(dtcs: List<String>): List<Graph> {
        val codes = dtcs.toSet()
        return graphs.filter { it.dtcs.intersect(codes).isNotEmpty() }
            .sortedWith(compareBy({ -(it.dtcs.intersect(codes).size) }, { it.family }))
    }

    /** Score every graph; best primary diagnosis first. */
    fun scoreAll(
        graphs: List<Graph>,
        observations: Map<String, Observation>,
        flags: Set<String> = emptySet(),
        dtcEvidence: List<DtcEvidence> = emptyList()
    ): List<ScoredGraph> {
        val allDtcs = dtcEvidence.map { it.code }
        return graphs.map { g ->
            ScoredGraph(g, scoreGraph(g, observations, flags, dtcEvidence, allDtcs))
        }.sortedWith(compareBy({ -(it.result.top?.diagnostic_score ?: 0.0) }, { it.graph.family }))
    }

    /** First unobserved test across the graphs, in graph order. */
    fun nextTest(
        graphs: List<Graph>,
        observedTestIds: Set<String>
    ): NextTest? {
        for (g in graphs) {
            for (t in g.tests) {
                if (t.id !in observedTestIds) {
                    return NextTest(g.family, t.id, t.kind, t.question)
                }
            }
        }
        return null
    }

    fun repairBasket(
        graph: Graph,
        scored: DiagnosisResult,
        observations: Map<String, Observation>
    ): RepairBasket = buildRepairBasket(graph, scored, observations)

    /**
     * Union of measurement recipes across the matched graphs, with
     * acquisition state — the contract the app calls before scanning.
     * Each recipe is the graph's declared signal map plus test_id, family
     * and acquired.
     */
    fun requiredSignals(
        graphs: List<Graph>,
        observations: Map<String, Observation>
    ): List<Map<String, Any>> {
        val seen = mutableMapOf<String, Map<String, Any>>()
        for (g in graphs) {
            for (t in g.tests) {
                if (t.kind != "obd_live") continue
                for (sigId in t.signals) {
                    if (sigId in seen) continue
                    val declared = g.signals[sigId] ?: continue
                    @Suppress("UNCHECKED_CAST")
                    val recipe = (declared.toAny() as Map<String, Any?>).toMutableMap()
                    recipe["test_id"] = t.id
                    recipe["family"] = g.family
                    recipe["acquired"] = t.id in observations
                    @Suppress("UNCHECKED_CAST")
                    seen[sigId] = recipe as Map<String, Any>
                }
            }
        }
        return seen.values.toList()
    }
}
