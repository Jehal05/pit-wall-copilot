import React from 'react';
import {StyleSheet, Text, View} from 'react-native';
import {RegulationAnswer} from '../api/types';

interface EvidenceSourcesScreenProps {
  loading: boolean;
  errorMessage: string | null;
  answer: RegulationAnswer | null;
}

/**
 * The "Evidence/Sources" P0 screen (spec 10.2 #5), backed by
 * get_regulation. Renders spec 50's structured evidence objects directly
 * ("SHALL NOT parse citations or numbers from generated prose") — every
 * field shown here (document_id/section_id/title/excerpt) comes straight
 * off `RegulationEvidence`, never through an LLM.
 *
 * Three distinct outcomes, matching UJ-4 (found), UJ-5 (hallucination
 * trap — found=false) and Section 29 (available=false, KB unavailable):
 */
export function EvidenceSourcesScreen({
  loading,
  errorMessage,
  answer,
}: EvidenceSourcesScreenProps): React.JSX.Element {
  if (loading) {
    return (
      <Text style={styles.loadingText} testID="evidence-loading">
        Searching the sporting-rules corpus…
      </Text>
    );
  }

  if (errorMessage) {
    return (
      <Text style={styles.errorText} testID="evidence-error">
        {errorMessage}
      </Text>
    );
  }

  if (!answer) {
    return (
      <Text style={styles.errorText} testID="evidence-empty">
        No regulation lookup has been run yet.
      </Text>
    );
  }

  if (!answer.available) {
    // Spec v1.1 repair 14 / Section 29 "Knowledge Base unavailable" — the
    // exact literal text, distinct from an ordinary found=false below.
    return (
      <Text style={styles.errorText} testID="evidence-unavailable">
        Regulation source unavailable. I won&apos;t infer the rule.
      </Text>
    );
  }

  if (!answer.found) {
    // UJ-5 "Hallucination trap": the exact specified refusal wording.
    return (
      <Text style={styles.unsupportedText} testID="evidence-unsupported">
        I don&apos;t have a rule supporting that in the available sporting-rule sources, so I won&apos;t use
        that assumption.
      </Text>
    );
  }

  return (
    <View testID="evidence-content">
      {answer.evidence.map(item => (
        <View key={`${item.document_id}-${item.section_id}`} style={styles.card} testID="evidence-card">
          <Text style={styles.rulebookLine}>
            RULEBOOK / {item.document_id.toUpperCase()} / {item.section_id.toUpperCase()}
          </Text>
          <Text style={styles.titleText}>{item.title}</Text>
          <Text style={styles.excerptText}>{item.excerpt}</Text>
          <Text style={styles.versionText}>
            {item.support_status} · v{item.version} · {item.source_path}
          </Text>
        </View>
      ))}
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
    fontWeight: '700',
  },
  unsupportedText: {
    color: '#d8a94f',
    fontSize: 18,
    lineHeight: 26,
  },
  card: {
    borderWidth: 1,
    borderColor: '#2a2e37',
    borderRadius: 8,
    backgroundColor: '#171a20',
    padding: 16,
    marginBottom: 14,
  },
  rulebookLine: {
    color: '#4fd1c5',
    fontSize: 14,
    fontWeight: '800',
    letterSpacing: 1,
    marginBottom: 8,
  },
  titleText: {
    color: '#f2f4f6',
    fontSize: 18,
    fontWeight: '700',
    marginBottom: 8,
  },
  excerptText: {
    color: '#e6e8eb',
    fontSize: 15,
    lineHeight: 22,
    marginBottom: 8,
  },
  versionText: {
    color: '#7d8492',
    fontSize: 13,
  },
});
