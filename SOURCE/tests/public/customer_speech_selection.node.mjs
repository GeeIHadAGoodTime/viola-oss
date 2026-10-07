import test from 'node:test';
import assert from 'node:assert/strict';
import { customerSpeechSelection, customerSpeechLanguagePatch } from '../../ui/react-app/src/utils/customerSpeechSelection.js';

const profile = {
  selection: { language: 'en-us', voice: 'af_heart' },
  locales: [
    { value: 'en-us', voices: ['af_heart'] },
    { value: 'es', voices: ['ef_dora'] },
    { value: 'zh', voices: ['zf_xiaobei'] },
  ],
};

test('first-use defaults display the admitted named voice without changing settings', () => {
  const settings = { tts_language: 'en-us', tts_voice: 'default' };
  assert.deepEqual(customerSpeechSelection(settings, profile), { language: 'en-us', voice: 'af_heart', voices: ['af_heart'] });
  assert.equal(settings.tts_voice, 'default');
});
test('language changes submit one matching pair and leave STT and answer locale alone', () => {
  const draft = { tts_language: 'en-us', tts_voice: 'af_heart', whisper_language: 'auto', locale: 'en-US' };
  for (const [locale, voice] of [['es', 'ef_dora'], ['zh', 'zf_xiaobei'], ['en-us', 'af_heart']]) {
    const changed = { ...draft, ...customerSpeechLanguagePatch(locale, draft.tts_voice, profile) };
    assert.equal(changed.tts_language, locale);
    assert.equal(changed.tts_voice, voice);
    assert.equal(changed.whisper_language, 'auto');
    assert.equal(changed.locale, 'en-US');
  }
});
test('unlisted dormant languages cannot be selected', () => {
  assert.equal(customerSpeechLanguagePatch('fr', 'af_heart', profile), null);
  assert.equal(customerSpeechLanguagePatch('es', 'af_heart', null), null);
});
test('later explicitly admitted voices remain selectable without a hard-coded product restriction', () => {
  const expanded = { ...profile, locales: [...profile.locales, { value: 'en-gb', voices: ['bf_emma', 'bm_george'] }] };
  assert.deepEqual(customerSpeechLanguagePatch('en-gb', 'bm_george', expanded), { tts_language: 'en-gb', tts_voice: 'bm_george' });
});
