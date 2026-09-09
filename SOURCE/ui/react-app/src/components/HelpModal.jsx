import React, { useState } from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../config';
import Modal, { secondaryButtonStyle } from './Modal';

// The version of the build the user is actually running, injected at build
// time from core/constants.py (VIOLA_VERSION) by vite.config.js — the same
// source BugReportModal attaches to reports. This line used to be the literal
// string "Version 1.0.0", which stayed at 1.0.0 while shipped builds moved on.
const APP_VERSION = import.meta.env.VITE_VIOLA_VERSION || '';

function TabButton({ active, children, onClick }) {
  return (
    <button
      onClick={onClick}
      style={{
        minHeight: '44px',
        boxSizing: 'border-box',
        padding: '10px 16px',
        borderRadius: '8px',
        border: 'none',
        backgroundColor: active ? THEME.colors.glassActive : 'transparent',
        color: active ? THEME.colors.textPrimary : THEME.colors.textSecondary,
        cursor: 'pointer',
        fontSize: '14px',
        fontWeight: 500,
        transition: 'all 0.15s ease',
      }}
    >
      {children}
    </button>
  );
}

TabButton.propTypes = {
  active: PropTypes.bool.isRequired,
  children: PropTypes.node.isRequired,
  onClick: PropTypes.func.isRequired,
};

function CommandRow({ command, description }) {
  return (
    <div
      style={{
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        padding: '10px 0',
        borderBottom: `1px solid ${THEME.colors.borderSubtle}`,
        gap: 16,
        flexWrap: 'wrap',
      }}
    >
      <code
        style={{
          backgroundColor: THEME.colors.bgSurface,
          padding: '6px 10px',
          borderRadius: '6px',
          color: THEME.colors.textPrimary,
          fontSize: '13px',
          fontFamily: 'monospace',
          whiteSpace: 'nowrap',
          flexShrink: 0,
        }}
      >
        {command}
      </code>
      <span style={{ color: THEME.colors.textSecondary, fontSize: '13px', textAlign: 'right', flex: '1 1 140px' }}>{description}</span>
    </div>
  );
}

CommandRow.propTypes = {
  command: PropTypes.string.isRequired,
  description: PropTypes.string.isRequired,
};

function SectionLabel({ children }) {
  return (
    <div
      style={{
        marginTop: 18,
        marginBottom: 4,
        fontSize: 11,
        fontWeight: 700,
        letterSpacing: 0.8,
        color: THEME.colors.textMuted,
      }}
    >
      {children}
    </div>
  );
}

SectionLabel.propTypes = { children: PropTypes.node.isRequired };

function AccordionItem({ title, children }) {
  const [open, setOpen] = useState(false);

  return (
    <div style={{ borderBottom: `1px solid ${THEME.colors.borderSubtle}` }}>
      <button
        onClick={() => setOpen(!open)}
        style={{
          width: '100%',
          padding: '14px 0',
          background: 'none',
          border: 'none',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          cursor: 'pointer',
          color: THEME.colors.textPrimary,
          fontSize: '14px',
          fontWeight: 500,
          textAlign: 'left',
        }}
      >
        {title}
        <span
          style={{
            transform: open ? 'rotate(180deg)' : 'rotate(0deg)',
            transition: 'transform 0.2s ease',
            color: THEME.colors.textMuted,
          }}
        >
          &#9660;
        </span>
      </button>
      {open && (
        <div
          style={{
            padding: '0 0 14px 0',
            color: THEME.colors.textSecondary,
            fontSize: '13px',
            lineHeight: 1.6,
          }}
        >
          {children}
        </div>
      )}
    </div>
  );
}

AccordionItem.propTypes = {
  title: PropTypes.string.isRequired,
  children: PropTypes.node.isRequired,
};

function CommandsTab() {
  return (
    <div>
      <p style={{ color: THEME.colors.textSecondary, marginBottom: 14, fontSize: 14, lineHeight: 1.6 }}>
        Two ways to talk to Viola: say <strong style={{ color: THEME.colors.textPrimary }}>&quot;Viola&quot;</strong> to wake her up, or hold push-to-talk (Space by default).
        You can also type into the chat tab. Examples by capability:
      </p>

      <SectionLabel>MUSIC</SectionLabel>
      <CommandRow command={'"Play [song or artist]"'} description="Start playing music" />
      <CommandRow command={'"Pause" / "Resume"'} description="Control playback" />
      <CommandRow command={'"Skip" / "Previous"'} description="Navigate tracks" />
      <CommandRow command={'"Volume 50"'} description="Set absolute volume" />
      <CommandRow command={'"Play [song] in the kitchen"'} description="Target a specific room" />
      <CommandRow command={'"Group living room with kitchen"'} description="Multiroom playback" />

      <SectionLabel>PHONE</SectionLabel>
      <CommandRow command={'"Call the pizza place"'} description="Outbound business call" />
      <CommandRow command={'"Book a haircut for Saturday"'} description="Make an appointment" />
      <CommandRow command={'"Hang up"'} description="End the current call" />

      <SectionLabel>CALENDAR & EMAIL</SectionLabel>
      <CommandRow command={'"What\'s on my calendar today?"'} description="Read agenda" />
      <CommandRow command={'"Schedule dinner Friday at 7"'} description="Create an event" />
      <CommandRow command={'"Read my unread email"'} description="Inbox summary" />
      <CommandRow command={'"Reply to Sarah"'} description="Draft a reply" />

      <SectionLabel>SMART HOME & BROWSER</SectionLabel>
      <CommandRow command={'"Turn off the bedroom lights"'} description="Smart-home control (if hub paired)" />
      <CommandRow command={'"Order groceries from Whole Foods"'} description="Browser automation task" />
      <CommandRow command={'"Find a flight to Chicago next week"'} description="Multi-step research" />

      <SectionLabel>MEMORY & CONTEXT</SectionLabel>
      <CommandRow command={'"Remember I prefer aisle seats"'} description="Store a preference" />
      <CommandRow command={'"What do you know about me?"'} description="Read your memory" />
      <CommandRow command={'"Forget my flight preferences"'} description="Remove a memory" />

      <SectionLabel>GENERAL</SectionLabel>
      <CommandRow command={'"What time is it?"'} description="Time, weather, quick facts" />
      <CommandRow command={'"Set a timer for 10 minutes"'} description="Timers and reminders" />
      <CommandRow command={'"Stop listening"'} description="Pause wake-word detection" />
    </div>
  );
}

function TroubleshootingTab() {
  return (
    <div>
      <AccordionItem title="Viola isn't responding to her name">
        <ul style={{ margin: 0, paddingLeft: 20 }}>
          <li>Check microphone permissions at the OS level.</li>
          <li>Background noise can mask the wake word — try speaking at conversational volume.</li>
          <li>Adjust wake sensitivity in Settings → Music &amp; Voice if she misses or fires too often.</li>
          <li>Hold push-to-talk (Space by default) any time you want to skip wake detection.</li>
        </ul>
      </AccordionItem>

      <AccordionItem title="Training a custom wake word keeps failing">
        <ul style={{ margin: 0, paddingLeft: 20 }}>
          <li>Pick a wake word with 2 to 4 syllables, like &quot;Athena&quot; or &quot;hey buddy&quot;, and avoid words you say often.</li>
          <li>Speak at a normal indoor volume. Say the whole phrase in about a second and a half, right after you press Record.</li>
          <li>Record 8 to 10 samples instead of the minimum 5, with a little variation in pace and distance from the mic.</li>
          <li>
            A grade of F after training means the automated quality check did not pass. That does not mean your
            recordings were bad: try training again with the same recordings before you re-record, since the
            same recordings sometimes pass on a later attempt.
          </li>
          <li>If it fails several times in a row for the same wake word, wait a few minutes and try again, or contact support.</li>
        </ul>
      </AccordionItem>

      <AccordionItem title="Music won't play">
        <ul style={{ margin: 0, paddingLeft: 20 }}>
          <li>Verify your music provider is connected in Settings → Services.</li>
          <li>For Spotify/YouTube Music, your subscription needs to be active.</li>
          <li>Local music: check that your library folder is set under Settings → Music.</li>
          <li>If a specific track fails, try a different one — region/licensing may apply.</li>
        </ul>
      </AccordionItem>

      <AccordionItem title="Audio plays in the wrong room (multiroom)">
        <ul style={{ margin: 0, paddingLeft: 20 }}>
          <li>Open the Rooms panel from the menu; confirm devices are online.</li>
          <li>Say &quot;Play in the kitchen&quot; to target a specific spoke.</li>
          <li>If a spoke is missing, re-pair it via Rooms → Add Speaker.</li>
        </ul>
      </AccordionItem>

      <AccordionItem title="Calendar / email isn't responding">
        <ul style={{ margin: 0, paddingLeft: 20 }}>
          <li>Connect Google in Settings → Services. Calendar and Gmail share one sign-in.</li>
          <li>If you revoked access, reconnect to get fresh tokens.</li>
          <li>Try &quot;What&apos;s on my calendar?&quot; — this verifies the read path.</li>
        </ul>
      </AccordionItem>

      <AccordionItem title="Phone calls fail or get rejected">
        <ul style={{ margin: 0, paddingLeg: 20, paddingLeft: 20 }}>
          <li>Outbound calling requires a paid plan and a verified number.</li>
          <li>Some businesses block AI-originated calls; Viola identifies herself as AI when enabled in Settings → Preferences.</li>
          <li>For recording, check Settings → Phone — recording is gated by jurisdiction.</li>
        </ul>
      </AccordionItem>

      <AccordionItem title="Browser-automation tasks stall">
        <ul style={{ margin: 0, paddingLeft: 20 }}>
          <li>Long-running tasks show progress in the Agentic stage mode.</li>
          <li>If a site requires login, Viola will pause and ask — answer in chat.</li>
          <li>CAPTCHAs and anti-bot challenges may block specific sites.</li>
        </ul>
      </AccordionItem>

      <AccordionItem title="Audio output is wrong / silent">
        <ul style={{ margin: 0, paddingLeft: 20 }}>
          <li>Set the output device in Settings → Music &amp; Voice.</li>
          <li>System mute / OS-level volume can mask Viola&apos;s output.</li>
          <li>For multiroom hub setups, the hub plays through its own device, not the desktop.</li>
        </ul>
      </AccordionItem>
    </div>
  );
}

function AboutTab() {
  return (
    <div>
      <div style={{ textAlign: 'center', marginBottom: 24 }}>
        <img
          src={`${import.meta.env.BASE_URL || '/'}viola_logo.png`}
          alt="Viola"
          style={{
            width: 84,
            height: 84,
            objectFit: 'contain',
            display: 'block',
            margin: '0 auto 12px',
          }}
        />
        <h3 style={{ color: THEME.colors.textPrimary, margin: '0 0 4px 0', fontWeight: 600, letterSpacing: -0.3 }}>
          Viola
        </h3>
        <p style={{ color: THEME.colors.textMuted, margin: 0, fontSize: 13 }}>
          Your cross-ecosystem personal AI
        </p>
      </div>

      <div
        style={{
          backgroundColor: THEME.colors.bgSurface,
          borderRadius: 12,
          padding: 16,
          marginBottom: 16,
        }}
      >
        <p style={{ color: THEME.colors.textSecondary, margin: 0, fontSize: 13, lineHeight: 1.6 }}>
          Viola is the assistant big tech is structurally incentivized to never build —
          one that works across Apple, Google, Microsoft, and Spotify equally, without
          favoring any single ecosystem. Talk or type. She handles music across every
          provider, calls and books on your behalf, manages your calendar and email,
          runs the browser when she has to, and remembers what matters between
          sessions.
        </p>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        <a
          href="/docs/USER_GUIDE.md"
          target="_blank"
          rel="noopener noreferrer"
          style={{
            display: 'block',
            padding: '12px 16px',
            backgroundColor: THEME.colors.bgSurface,
            borderRadius: 8,
            color: THEME.colors.textPrimary,
            textDecoration: 'none',
            fontSize: 14,
            transition: 'background-color 0.15s ease',
          }}
          onMouseOver={(e) => (e.currentTarget.style.backgroundColor = THEME.colors.glassHover)}
          onMouseOut={(e) => (e.currentTarget.style.backgroundColor = THEME.colors.bgSurface)}
        >
          Full Documentation &rarr;
        </a>

        <a
          href="/docs/legal/CORE_PRIVACY_POLICY.md"
          target="_blank"
          rel="noopener noreferrer"
          style={{
            display: 'block',
            padding: '12px 16px',
            backgroundColor: THEME.colors.bgSurface,
            borderRadius: 8,
            color: THEME.colors.textSecondary,
            textDecoration: 'none',
            fontSize: 14,
            transition: 'background-color 0.15s ease',
          }}
          onMouseOver={(e) => (e.currentTarget.style.backgroundColor = THEME.colors.glassHover)}
          onMouseOut={(e) => (e.currentTarget.style.backgroundColor = THEME.colors.bgSurface)}
        >
          Privacy Policy
        </a>

        <a
          href="/docs/legal/CORE_TERMS_OF_SERVICE.md"
          target="_blank"
          rel="noopener noreferrer"
          style={{
            display: 'block',
            padding: '12px 16px',
            backgroundColor: THEME.colors.bgSurface,
            borderRadius: 8,
            color: THEME.colors.textSecondary,
            textDecoration: 'none',
            fontSize: 14,
            transition: 'background-color 0.15s ease',
          }}
          onMouseOver={(e) => (e.currentTarget.style.backgroundColor = THEME.colors.glassHover)}
          onMouseOut={(e) => (e.currentTarget.style.backgroundColor = THEME.colors.bgSurface)}
        >
          Terms of Service
        </a>
      </div>

      {APP_VERSION && (
        <p style={{ color: THEME.colors.textMuted, fontSize: 12, textAlign: 'center', marginTop: 24 }}>
          Version {APP_VERSION}
        </p>
      )}
    </div>
  );
}

export default function HelpModal({ isOpen, onClose }) {
  const [activeTab, setActiveTab] = useState('commands');

  const renderTabContent = () => {
    switch (activeTab) {
      case 'commands':
        return <CommandsTab />;
      case 'troubleshooting':
        return <TroubleshootingTab />;
      case 'about':
        return <AboutTab />;
      default:
        return null;
    }
  };

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title="Help"
      footer={
        <button onClick={onClose} style={secondaryButtonStyle}>
          Close
        </button>
      }
    >
      <div
        style={{
          display: 'flex',
          gap: 4,
          marginBottom: 20,
          padding: 4,
          backgroundColor: THEME.colors.bgSurface,
          borderRadius: 12,
        }}
      >
        <TabButton active={activeTab === 'commands'} onClick={() => setActiveTab('commands')}>
          Commands
        </TabButton>
        <TabButton active={activeTab === 'troubleshooting'} onClick={() => setActiveTab('troubleshooting')}>
          Troubleshooting
        </TabButton>
        <TabButton active={activeTab === 'about'} onClick={() => setActiveTab('about')}>
          About
        </TabButton>
      </div>

      {renderTabContent()}
    </Modal>
  );
}

HelpModal.propTypes = {
  isOpen: PropTypes.bool.isRequired,
  onClose: PropTypes.func.isRequired,
};
