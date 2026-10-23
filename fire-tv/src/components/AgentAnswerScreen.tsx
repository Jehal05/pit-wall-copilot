import React from 'react';
import {StyleSheet, Text, View} from 'react-native';

export interface EvidenceLine {
  /** Short all-caps label per spec 10.1/UJ-2's evidence-panel mockup
   * (e.g. "LIVE STATE", "MODEL", "TRAFFIC"). */
  label: string;
  value: string;
}

interface AgentAnswerScreenProps {
  loading: boolean;
  errorMessage: string | null;
  narrative: string | null;
  /** Spec v1.1 repair 14: true when this narrative is the exact Bedrock-
   * unavailable degraded text rather than a real answer. */
  degraded?: boolean;
  /** Structured evidence backing the narrative — spec 50: "The Fire TV
   * evidence panel SHALL render structured evidence objects returned by
   * tools. It SHALL NOT parse citations or numbers from generated prose."
   * These lines are built by the caller directly from a typed API
   * response (ExplanationResult.grounded_facts, AgentQueryResult.evidence,
   * RaceChangesResult.explained_changes), never extracted from
   * `narrative` itself. */
  evidence: EvidenceLine[];
}

/**
 * The "Agent Answer" P0 screen (spec 10.2 #4) — used for WHY? (UJ-2,
 * backed by explain_alert / queryAgent) and WHAT CHANGED? (UJ-6, backed
 * by GET .../changes). Both are "a narrative plus the structured evidence
 * it's grounded in," so they share this one component rather than two
 * near-duplicates.
 */
export function AgentAnswerScreen({
  loading,
  errorMessage,
  narrative,
  degraded = false,
  evidence,
}: AgentAnswerScreenProps): React.JSX.Element {
  if (loading) {
    return (
      <Text style={styles.loadingText} testID="agent-answer-loading">
        Thinking…
      </Text>
    );
  }

  if (errorMessage) {
    return (
      <Text style={styles.errorText} testID="agent-answer-error">
        {errorMessage}
      </Text>
    );
  }

  return (
    <View testID="agent-answer-content">
      <Text style={[styles.narrative, degraded && styles.narrativeDegraded]} testID="agent-answer-narrative">
        {narrative}
      </Text>
      {evidence.length > 0 && (
        <View style={styles.evidenceContainer} testID="agent-answer-evidence">
          {evidence.map((item, index) => (
            <View key={`${item.label}-${index}`} style={styles.evidenceRow}>
              <Text style={styles.evidenceLabel}>{item.label}</Text>
              <Text style={styles.evidenceValue}>{item.value}</Text>
            </View>
          ))}
        </View>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  loadingText: {
    color: '#7d8492',
    fontSize: 18,
  },
  errorText: {
    color: '#e05a5a',
    fontSize: 18,
    fontWeight: '600',
  },
  narrative: {
    color: '#e6e8eb',
    fontSize: 22,
    lineHeight: 30,
    marginBottom: 20,
  },
  narrativeDegraded: {
    color: '#d8a94f',
    fontStyle: 'italic',
  },
  evidenceContainer: {
    borderTopWidth: 1,
    borderTopColor: '#2a2e37',
    paddingTop: 16,
  },
  evidenceRow: {
    flexDirection: 'row',
    marginBottom: 10,
  },
  evidenceLabel: {
    color: '#7d8492',
    fontSize: 14,
    fontWeight: '700',
    letterSpacing: 1,
    width: 160,
  },
  evidenceValue: {
    flex: 1,
    color: '#e6e8eb',
    fontSize: 16,
  },
});
