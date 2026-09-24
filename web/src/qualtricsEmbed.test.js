/**
 * Tests for the Qualtrics embed snippet that researchers copy into a survey
 * (docs/survey-integration/qualtrics/embed_chatbot.js).
 *
 * This snippet is the least defended code in the project: it runs inside
 * Qualtrics, not here, so nothing else exercises it. It has broken a live
 * survey three separate ways — jQuery missing from the theme, the iframe
 * appended before the question rendered, and an unresolved participant_id —
 * and every one of them presents as the same blank page with no error.
 *
 * So the real file is read from disk and executed here against jsdom, rather
 * than a copy being re-implemented in the test. If the snippet drifts, these
 * fail.
 */

import { readFileSync } from 'fs';
import { dirname, resolve } from 'path';
import { fileURLToPath } from 'url';
import { describe, expect, it, beforeEach } from 'vitest';

const SNIPPET_PATH = resolve(
  dirname(fileURLToPath(import.meta.url)),
  '../../docs/survey-integration/qualtrics/embed_chatbot.js'
);

const source = readFileSync(SNIPPET_PATH, 'utf8');

/** Substitute the placeholders a researcher fills in, plus Qualtrics piping. */
function configured() {
  return source
    .replaceAll('<STUDY-NAME>', 'qualtrics_pilot')
    .replaceAll('<BOT-NAME>', 'survey_ctx_treatment')
    .replaceAll('<SURVEY-ID>', 'SV_pilot_test')
    .replaceAll('<USER-GROUP>', 'treatment')
    .replaceAll('<YOUR-CHATBOTLAB-DOMAIN>', 'dev.bot.wwbp.org')
    .replaceAll('${e://Field/ResponseID}', 'R_abc123');
}

/**
 * Run the snippet the way Qualtrics does: it registers a callback, which is
 * then invoked with the question object as `this`.
 */
function runSnippet({ container, code = configured() }) {
  const handlers = { addOnload: [], addOnReady: [] };
  const Qualtrics = {
    SurveyEngine: {
      addOnload: fn => handlers.addOnload.push(fn),
      addOnReady: fn => handlers.addOnReady.push(fn),
    },
  };

  // eslint-disable-next-line no-new-func
  new Function('Qualtrics', code)(Qualtrics);

  const question = {
    getQuestionTextContainer: () => container,
    questionContainer: container,
  };
  [...handlers.addOnload, ...handlers.addOnReady].forEach(fn =>
    fn.call(question)
  );

  return handlers;
}

describe('Qualtrics embed snippet', () => {
  let container;

  beforeEach(() => {
    document.body.innerHTML = '';
    container = document.createElement('div');
    document.body.appendChild(container);
  });

  it('appends an iframe to the question container', () => {
    runSnippet({ container });

    const iframe = container.querySelector('iframe');
    expect(iframe).not.toBeNull();
  });

  it('builds a chatbot URL carrying every parameter the app needs', () => {
    runSnippet({ container });

    const url = new URL(container.querySelector('iframe').src);
    expect(url.origin + url.pathname).toBe(
      'https://dev.bot.wwbp.org/conversation'
    );
    expect(Object.fromEntries(url.searchParams)).toEqual({
      bot_name: 'survey_ctx_treatment',
      conversation_id: 'R_abc123',
      participant_id: 'R_abc123',
      study_name: 'qualtrics_pilot',
      user_group: 'treatment',
      survey_id: 'SV_pilot_test',
    });
  });

  it('runs where the Qualtrics theme provides no jQuery', () => {
    // The failure that broke a live survey: the snippet died at the element
    // factory with "jQuery is not defined", before creating the iframe, and
    // the page looked identical to no JavaScript having run.
    expect(globalThis.jQuery).toBeUndefined();
    expect(() => runSnippet({ container })).not.toThrow();
    expect(container.querySelector('iframe')).not.toBeNull();
  });

  it('never calls jQuery', () => {
    // Comments are stripped first: the snippet explains in prose why it avoids
    // jQuery, and that explanation must not read as a usage.
    const code = source
      .replace(/\/\*[\s\S]*?\*\//g, '')
      .replace(/^\s*\/\/.*$/gm, '');
    expect(code).not.toMatch(/jQuery\s*\(|\$\s*\(/);
  });

  it('appends from addOnReady, after the question has rendered', () => {
    // addOnload runs before the question is displayed, and Qualtrics discards
    // anything appended then.
    const handlers = runSnippet({ container });
    expect(handlers.addOnReady).toHaveLength(1);
    expect(handlers.addOnload).toHaveLength(0);
  });

  it('gives the iframe a visible height', () => {
    // Set via style, not an HTML attribute: an attribute carrying CSS units
    // is ignored, which renders a zero-height iframe that looks like failure.
    const iframe = container.querySelector('iframe');
    runSnippet({ container });
    const appended = container.querySelector('iframe');
    expect(appended.style.height).toMatch(/^\d+px$/);
    expect(appended.style.width).toBeTruthy();
    expect(iframe).toBeNull();
  });

  it('survives a question container that is not available', () => {
    expect(() => runSnippet({ container: null })).not.toThrow();
  });

  it('percent-encodes values so a stray character cannot break the URL', () => {
    const code = configured().replaceAll('R_abc123', 'R_a b&c=1');
    runSnippet({ container, code });

    const url = new URL(container.querySelector('iframe').src);
    expect(url.searchParams.get('participant_id')).toBe('R_a b&c=1');
  });
});
