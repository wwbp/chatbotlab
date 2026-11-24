import { render, screen, fireEvent } from '@testing-library/react';
import Avatar from './Avatar';

const ok = body =>
  Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
const fail = () =>
  Promise.resolve({ ok: false, json: () => Promise.resolve({}) });

const upload = container => {
  const photo = new File(['x'], 'me.jpg', { type: 'image/jpeg' });
  fireEvent.change(container.querySelector('input[type="file"]'), {
    target: { files: [photo] },
  });
  fireEvent.click(screen.getByRole('button', { name: /upload/i }));
};

beforeEach(() => {
  window.history.pushState(
    {},
    '',
    '/avatar-upload?bot_name=my-bot&conversation_id=R_1&participant_id=p1'
  );
});

afterEach(() => vi.restoreAllMocks());

test('asks for the upload URL for this bot', async () => {
  // The backend only lets participants upload for bots with participant avatars,
  // so it needs to know which bot.
  const fetchMock = vi
    .spyOn(globalThis, 'fetch')
    .mockImplementation(() => ok({ s3_url: 'https://s3/put' }));
  const { container } = render(<Avatar />);
  upload(container);
  expect(await screen.findByText(/upload successful/i)).toBeInTheDocument();
  expect(fetchMock.mock.calls[0][0]).toContain('bot_name=my-bot');
});

test('tells the participant when the upload failed', async () => {
  vi.spyOn(globalThis, 'fetch')
    .mockImplementationOnce(() => ok({ s3_url: 'https://s3/put' }))
    .mockImplementationOnce(() => ok({}))
    .mockImplementationOnce(fail);
  const { container } = render(<Avatar />);
  upload(container);
  expect(await screen.findByText(/please try again/i)).toBeInTheDocument();
});
