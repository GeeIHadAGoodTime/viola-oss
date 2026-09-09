import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';
import en from './en.json';

const resources = {
  en: {
    translation: en,
  },
};

if (!i18n.isInitialized) {
  i18n
    .use(initReactI18next)
    .init({
      resources,
      lng: 'en',
      fallbackLng: 'en',
      supportedLngs: ['en'],
      interpolation: {
        escapeValue: false,
      },
      returnNull: false,
    });
}

if (typeof document !== 'undefined') {
  document.documentElement.lang = i18n.language || 'en';
  document.documentElement.dir = i18n.dir(i18n.language || 'en');
}

export { resources };
export default i18n;
