// Only server-admitted artifact inventory supplies output-language choices.
export function customerSpeechSelection(settings, profile) {
  const language = settings.tts_language || profile?.selection?.language || '';
  const savedVoice = settings.tts_voice || 'default';
  const voice = savedVoice === 'default' && language === profile?.selection?.language
    ? profile.selection.voice : savedVoice;
  const route = profile?.locales?.find(item => item.value === language);
  return { language, voice, voices: route?.voices || [] };
}

export function customerSpeechLanguagePatch(language, currentVoice, profile) {
  const voices = profile?.locales?.find(item => item.value === language)?.voices || [];
  if (!voices.length) return null;
  return { tts_language: language, tts_voice: voices.includes(currentVoice) ? currentVoice : voices[0] };
}
