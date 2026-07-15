/**
 * Cast-safety classifier — frontend mirror of backend ``fpulse.types.cast_safety``.
 *
 * Operates on plain native-type names (the strings the source emits to
 * the Mapping tab, e.g. ``VARCHAR(255)``, ``NUMERIC(18,2)``, ``int4``)
 * rather than full canonical FPField objects. That's enough resolution
 * to render the per-row ✓ / ⚠ / ✕ glyph instantly without a network
 * hop — the runtime always re-classifies authoritatively on execute,
 * so this is a UX hint, not a contract.
 *
 * Mirror of the backend taxonomy:
 *   safe            lossless cast (INT → BIGINT, VARCHAR → VARCHAR widened)
 *   semantic_lossy  bytes fit, business meaning narrows (JSON → STRING)
 *   lossy           value-level loss possible (BIGINT → INT, narrowing varchar)
 *   impossible      no valid cast in principle (BLOB → DATE)
 */

export type CastTier = 'safe' | 'semantic_lossy' | 'lossy' | 'impossible';

/** Coarse logical kind we can derive from a free-form native-type string. */
type Kind =
    | 'int'
    | 'decimal'
    | 'float'
    | 'string'
    | 'bool'
    | 'date'
    | 'time'
    | 'timestamp'
    | 'json'
    | 'binary'
    | 'unknown';

/**
 * Best-effort parse of a native-type token into ``{kind, length}``.
 *
 * The Mapping tab doesn't have the full canonical FPField in hand, just
 * the source's DuckDB type and the destination's DB type. We pull out
 * just enough (kind + length/precision) to compute the dominant tier.
 */
function parseNative(raw: string): { kind: Kind; length?: number; precision?: number; scale?: number } {
    const t = (raw || '').toLowerCase().trim();
    if (!t) return { kind: 'unknown' };

    // Strings — varchar(n) / char(n) / text / uuid / inet / cidr.
    if (/varchar|character\s+varying|char\b|text|string|uuid|inet|cidr/.test(t)) {
        const m = t.match(/\((\d+)\)/);
        return { kind: 'string', length: m ? parseInt(m[1], 10) : undefined };
    }

    // Decimal / numeric — numeric(p,s) or decimal.
    if (/numeric|decimal/.test(t)) {
        const m = t.match(/\((\d+)(?:\s*,\s*(\d+))?\)/);
        if (m) return { kind: 'decimal', precision: parseInt(m[1], 10), scale: m[2] ? parseInt(m[2], 10) : 0 };
        return { kind: 'decimal' };
    }

    // Floats — real / float / double.
    if (/real|float|double/.test(t)) return { kind: 'float' };

    // Integers — smallint / integer / bigint / int2 / int4 / int8 / serial.
    if (/smallint|bigint|integer\b|^int\b|int2|int4|int8|serial/.test(t)) return { kind: 'int' };

    if (/bool/.test(t)) return { kind: 'bool' };
    if (/timestamp/.test(t)) return { kind: 'timestamp' };
    if (/^date\b/.test(t)) return { kind: 'date' };
    if (/\btime\b/.test(t) && !/timestamp/.test(t)) return { kind: 'time' };
    if (/jsonb?/.test(t)) return { kind: 'json' };
    if (/bytea|binary|blob/.test(t)) return { kind: 'binary' };

    return { kind: 'unknown' };
}

/**
 * Classify a source→target cast. Returns ``null`` if either side is
 * unparsable — the UI falls back to no glyph in that case rather than
 * misleading the operator.
 */
export function classifyCastUI(
    sourceType: string | undefined,
    targetType: string | undefined,
): { tier: CastTier; reason?: string } | null {
    if (!sourceType || !targetType) return null;
    const s = parseNative(sourceType);
    const t = parseNative(targetType);
    if (s.kind === 'unknown' || t.kind === 'unknown') return null;

    // Same kind: only difference can come from params.
    if (s.kind === t.kind) {
        if (s.kind === 'string' && s.length != null && t.length != null) {
            if (t.length >= s.length) return { tier: 'safe' };
            return { tier: 'lossy', reason: `string length narrows ${s.length} → ${t.length}` };
        }
        if (s.kind === 'decimal' && s.precision != null && t.precision != null) {
            const ss = s.scale ?? 0, ts = t.scale ?? 0;
            if (t.precision >= s.precision && ts >= ss) return { tier: 'safe' };
            return { tier: 'lossy', reason: `decimal narrows ${s.precision},${ss} → ${t.precision},${ts}` };
        }
        return { tier: 'safe' };
    }

    // Numeric widening / narrowing matrix.
    if (s.kind === 'int' && t.kind === 'decimal') return { tier: 'safe' };
    if (s.kind === 'decimal' && t.kind === 'int') return { tier: 'lossy', reason: 'fractional part truncated' };
    if (s.kind === 'int' && t.kind === 'float') return { tier: 'lossy', reason: 'precision loss for integers > 2^53' };
    if (s.kind === 'float' && t.kind === 'int') return { tier: 'lossy', reason: 'fractional part truncated' };
    if (s.kind === 'float' && t.kind === 'decimal') return { tier: 'lossy', reason: 'float→decimal can drift in low-order digits' };
    if (s.kind === 'decimal' && t.kind === 'float') return { tier: 'lossy', reason: 'decimal precision narrows to float mantissa' };

    // Date / time pairings.
    if (s.kind === 'date' && t.kind === 'timestamp') return { tier: 'safe' };
    if (s.kind === 'timestamp' && t.kind === 'date') return { tier: 'lossy', reason: 'time component dropped' };
    if (s.kind === 'time' && t.kind === 'timestamp') return { tier: 'lossy', reason: 'date component must be synthesized' };
    if (s.kind === 'timestamp' && t.kind === 'time') return { tier: 'lossy', reason: 'date component dropped' };

    // → string: semantic-lossy in most cases, lossy from binary.
    if (t.kind === 'string') {
        if (s.kind === 'json') return { tier: 'semantic_lossy', reason: 'JSON serialized as string — parseability + nested addressing lost' };
        if (s.kind === 'binary') return { tier: 'lossy', reason: 'binary→string is encoding-dependent' };
        return { tier: 'semantic_lossy', reason: 'downcast to string discards type-level constraints' };
    }

    // string → typed: lossy at runtime parse.
    if (s.kind === 'string') {
        if (t.kind === 'int' || t.kind === 'float' || t.kind === 'decimal') {
            return { tier: 'lossy', reason: 'string→numeric parses at runtime; invalid values null' };
        }
        if (t.kind === 'date' || t.kind === 'time' || t.kind === 'timestamp') {
            return { tier: 'lossy', reason: 'string→temporal parses at runtime; format-dependent' };
        }
        if (t.kind === 'bool') {
            return { tier: 'lossy', reason: 'string→boolean depends on accepted truthy/falsy values' };
        }
    }

    // JSON ↔ structured-ish — current UI doesn't see STRUCT/LIST/MAP separately.
    // (TS already narrowed t.kind ≠ 'string' via the `if (t.kind === 'string')`
    // branch above; the explicit check would have been a no-op.)
    if (s.kind === 'json') return { tier: 'semantic_lossy', reason: 'engine-specific JSON→typed mapping' };

    return { tier: 'impossible', reason: `no defined cast from ${s.kind} to ${t.kind}` };
}

/** Glyph + tier metadata for badge rendering. */
export const TIER_META: Record<CastTier, { glyph: string; label: string; tone: 'ok' | 'warn' | 'error' }> = {
    safe: { glyph: '✓', label: 'Safe', tone: 'ok' },                  // ✓
    semantic_lossy: { glyph: '⚠', label: 'Semantic-lossy', tone: 'warn' }, // ⚠
    lossy: { glyph: '⚠', label: 'Lossy', tone: 'warn' },               // ⚠ (different tooltip)
    impossible: { glyph: '✕', label: 'Impossible', tone: 'error' },     // ✕
};
