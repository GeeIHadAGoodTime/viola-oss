// Only server-admitted artifact inventory supplies output-language choices.
export function customerSpeechSelection(settings, profile) {
  const language = settings.tts_language || profile?.selection?.language || '';
  const savedVoice = settings.tts_voice || 'default';
  const voice = savedVoice === 'default' && language === profile?.selection?.language
    ? profile.selection.voice : savedVoice;
  const route = profile?.locales?.find(item => item.value === language);
  const voices = route?.voices || [];
  return { language, voice, voices, voiceOptions: customerVoiceOptions(route) };
}

// Picker entries: the backend's friendly labels ("Heart (female, default)"),
// saving the same raw Kokoro id. Older backends send ids only.
export function customerVoiceOptions(route) {
  const voices = route?.voices || [];
  const labels = new Map((route?.voice_options || []).map(item => [item?.value, item?.label]));
  return voices.map(voice => ({ value: voice, label: labels.get(voice) || voice }));
}

export function customerSpeechLanguagePatch(language, currentVoice, profile) {
  const voices = profile?.locales?.find(item => item.value === language)?.voices || [];
  if (!voices.length) return null;
  return { tts_language: language, tts_voice: voices.includes(currentVoice) ? currentVoice : voices[0] };
}
