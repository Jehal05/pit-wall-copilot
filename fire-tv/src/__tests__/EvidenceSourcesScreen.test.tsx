import React from 'react';
import {render, screen} from '@testing-library/react-native';
import {EvidenceSourcesScreen} from '../components/EvidenceSourcesScreen';
import {RegulationAnswer} from '../api/types';

describe('EvidenceSourcesScreen', () => {
  it('shows a loading state', () => {
    render(<EvidenceSourcesScreen loading errorMessage={null} answer={null} />);
    expect(screen.getByTestId('evidence-loading')).toBeTruthy();
  });

  it('renders structured evidence for a found=true answer (UJ-4)', () => {
    const answer: RegulationAnswer = {
      query: 'What is an undercut?',
      found: true,
      available: true,
      evidence: [
        {
          document_id: 'reg-undercut-001',
          section_id: 'R12',
          title: 'Undercut definition',
          version: '1.0',
          support_status: 'SUPPORTED',
          excerpt: 'An undercut is pitting first to emerge ahead of a rival on fresher tires.',
          source_path: 'docs/sporting-rules/regulations/undercut.md',
        },
      ],
    };
    render(<EvidenceSourcesScreen loading={false} errorMessage={null} answer={answer} />);
    expect(screen.getByText('RULEBOOK / REG-UNDERCUT-001 / R12')).toBeTruthy();
    expect(screen.getByText('Undercut definition')).toBeTruthy();
    expect(
      screen.getByText('An undercut is pitting first to emerge ahead of a rival on fresher tires.'),
    ).toBeTruthy();
  });

  it('shows the exact UJ-5 hallucination-trap refusal wording for found=false, available=true', () => {
    const answer: RegulationAnswer = {
      query: 'Drivers can ignore the tire rule after lap 40, right?',
      found: false,
      available: true,
      evidence: [],
    };
    render(<EvidenceSourcesScreen loading={false} errorMessage={null} answer={answer} />);
    expect(screen.getByTestId('evidence-unsupported')).toBeTruthy();
    expect(
      screen.getByText(
        "I don't have a rule supporting that in the available sporting-rule sources, so I won't use that assumption.",
      ),
    ).toBeTruthy();
  });

  it('shows the exact Section 29 degraded text when available=false', () => {
    const answer: RegulationAnswer = {
      query: 'What is an undercut?',
      found: false,
      available: false,
      evidence: [],
    };
    render(<EvidenceSourcesScreen loading={false} errorMessage={null} answer={answer} />);
    expect(screen.getByTestId('evidence-unavailable')).toBeTruthy();
    expect(screen.getByText("Regulation source unavailable. I won't infer the rule.")).toBeTruthy();
  });
});
