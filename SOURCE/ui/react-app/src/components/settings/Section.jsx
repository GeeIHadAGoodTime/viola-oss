import React from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../../config';

const theme = THEME;

/**
 * A settings section with an uppercase title and a bordered content container.
 * @param {Object} props
 * @param {string} props.title - Section heading text (rendered uppercase)
 * @param {React.ReactNode} props.children - Section content
 */
const Section = React.memo(({ title, children }) => (
  <section style={{ marginBottom: '28px' }} aria-label={title}>
    <h3 style={{
      fontSize: '11px',
      fontWeight: 600,
      textTransform: 'uppercase',
      letterSpacing: '1px',
      color: theme.colors.textMuted,
      marginTop: 0,
      marginBottom: '12px',
      paddingLeft: '20px',
    }}>
      {title}
    </h3>
    <div style={{
      backgroundColor: theme.colors.bgElevated,
      borderRadius: '16px',
      border: `1px solid ${theme.colors.borderSubtle}`,
      overflow: 'hidden',
    }}>
      {children}
    </div>
  </section>
));

Section.propTypes = {
  title: PropTypes.string.isRequired,
  children: PropTypes.node.isRequired,
};

export default Section;
