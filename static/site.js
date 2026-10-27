'use strict';
const form = document.querySelector('.quote-form');
if (form) {
  let tracked = false;
  form.addEventListener('input', () => {
    if (tracked) return;
    tracked = true;
    fetch('/events', {method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content},
      body: JSON.stringify({name: 'form_start'})}).catch(() => {});
  });
  const email = form.querySelector('#email');
  const phone = form.querySelector('#phone');
  const checkContact = () => email.setCustomValidity(
    email.value.trim() || phone.value.trim() ? '' : 'Please provide an email address or phone number.');
  email.addEventListener('input', checkContact);
  phone.addEventListener('input', checkContact);
  checkContact();
  form.addEventListener('submit', () => {
    const button = form.querySelector('[type="submit"]');
    button.disabled = true;
    button.textContent = 'Submitting…';
  });
}
const summary = document.querySelector('.error-summary');
if (summary) summary.focus();
document.querySelectorAll('.delete-lead').forEach(button => {
  button.addEventListener('click', event => {
    if (!confirm('Permanently delete this inquiry and its notification? This cannot be undone.')) event.preventDefault();
  });
});
