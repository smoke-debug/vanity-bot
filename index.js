const fs = require('node:fs');
const path = require('node:path');
const {
  Client,
  GatewayIntentBits,
  PermissionsBitField,
  ChannelType,
  EmbedBuilder,
  ActionRowBuilder,
  ButtonBuilder,
  ButtonStyle,
} = require('discord.js');

// =========================
// EASY CONFIG
// =========================
const TOKEN = process.env.DISCORD_TOKEN;
const PREFIX = process.env.PREFIX || '*';
const DATA_FILE = process.env.DATA_FILE || './bot-data.json';
const TEMP_VC_DELETE_DELAY_MS = Number(process.env.TEMP_VC_DELETE_DELAY_MS || 500);

if (!TOKEN) {
  console.error('Missing DISCORD_TOKEN. Add it in Railway Variables.');
  process.exit(1);
}

// =========================
// CLIENT
// =========================
const client = new Client({
  intents: [
    GatewayIntentBits.Guilds,
    GatewayIntentBits.GuildMembers,
    GatewayIntentBits.GuildMessages,
    GatewayIntentBits.GuildVoiceStates,
    GatewayIntentBits.MessageContent,
  ],
});

// =========================
// SIMPLE JSON STORAGE
// =========================
let db = {
  guilds: {},
};

function ensureDataFile() {
  const dir = path.dirname(DATA_FILE);
  if (dir && dir !== '.') fs.mkdirSync(dir, { recursive: true });

  if (!fs.existsSync(DATA_FILE)) {
    fs.writeFileSync(DATA_FILE, JSON.stringify(db, null, 2));
  }
}

function loadDb() {
  try {
    ensureDataFile();
    const raw = fs.readFileSync(DATA_FILE, 'utf8');
    db = JSON.parse(raw || '{"guilds":{}}');
    if (!db.guilds) db.guilds = {};
  } catch (error) {
    console.error('Failed to load data file. Starting with empty data.', error);
    db = { guilds: {} };
  }
}

function saveDb() {
  try {
    const dir = path.dirname(DATA_FILE);
    if (dir && dir !== '.') fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(DATA_FILE, JSON.stringify(db, null, 2));
  } catch (error) {
    console.error('Failed to save data file:', error);
  }
}

function getGuildData(guildId) {
  if (!db.guilds[guildId]) {
    db.guilds[guildId] = {
      uwuTargets: [],
      vm: null,
      tempVcs: {},
      economy: { users: {} },
    };
    saveDb();
  }

  if (!db.guilds[guildId].uwuTargets) db.guilds[guildId].uwuTargets = [];
  if (!db.guilds[guildId].tempVcs) db.guilds[guildId].tempVcs = {};
  if (!db.guilds[guildId].economy) db.guilds[guildId].economy = { users: {} };
  if (!db.guilds[guildId].economy.users) db.guilds[guildId].economy.users = {};

  return db.guilds[guildId];
}

loadDb();

// =========================
// SMALL HELPERS
// =========================
function hasManagerPerm(member) {
  return member.permissions.has(PermissionsBitField.Flags.Administrator)
    || member.permissions.has(PermissionsBitField.Flags.ManageGuild)
    || member.permissions.has(PermissionsBitField.Flags.ManageChannels);
}

function hasUwUPerm(member) {
  return member.permissions.has(PermissionsBitField.Flags.Administrator)
    || member.permissions.has(PermissionsBitField.Flags.ManageGuild)
    || member.permissions.has(PermissionsBitField.Flags.ManageMessages);
}

function cleanName(name) {
  return String(name || 'User')
    .replace(/[^a-zA-Z0-9 _.-]/g, '')
    .trim()
    .slice(0, 24) || 'User';
}

function getVcUsername(member) {
  // Force temp VC names to use the account username first, not a generic nickname/display fallback.
  // Example: ernesto's Public VC
  return cleanName(
    member?.user?.username
    || member?.user?.globalName
    || member?.displayName
    || 'User'
  );
}

function parseUserId(text) {
  if (!text) return null;
  const match = text.match(/^<@!?(\d{16,25})>$/) || text.match(/^(\d{16,25})$/);
  return match ? match[1] : null;
}

function syntaxEmbed(syntax, extra = '') {
  return new EmbedBuilder()
    .setTitle('Incorrect Syntax')
    .setDescription(['**Syntax:**', `\`${syntax}\``, extra ? `\n${extra}` : ''].join('\n'));
}

function economyErrorEmbed(title, description) {
  return new EmbedBuilder().setTitle(title).setDescription(description);
}

function premiumEmbed(title, description, color = 0xff8a00) {
  const text = Array.isArray(description) ? description.join('\n') : String(description || '');
  return new EmbedBuilder()
    .setColor(color)
    .setTitle(title)
    .setDescription(text)
    .setFooter({ text: 'Smoke Bucks Economy • Premium Games' })
    .setTimestamp();
}

// Safety aliases in case an older command block references a misspelled embed helper.
const premiumGameEmbed = premiumEmbed;
const premimumEmbed = premiumEmbed;

async function animatedReply(message, frames = [], finalPayload = null, delayMs = 900) {
  // Sends one reply, edits it through each frame, then edits to the final result.
  // This keeps games feeling animated without spamming the channel.
  const safeFrames = Array.isArray(frames) ? frames.filter(Boolean) : [];
  let sent;

  try {
    if (safeFrames.length > 0) {
      sent = await message.reply({ ...safeFrames[0], allowedMentions: { parse: [] } });
    } else if (finalPayload) {
      return await message.reply({ ...finalPayload, allowedMentions: { parse: [] } });
    } else {
      return null;
    }

    for (const frame of safeFrames.slice(1)) {
      await sleep(delayMs);
      await sent.edit({ ...frame, allowedMentions: { parse: [] } }).catch(() => null);
    }

    if (finalPayload) {
      await sleep(delayMs);
      await sent.edit({ ...finalPayload, allowedMentions: { parse: [] } }).catch(() => null);
    }

    return sent;
  } catch (error) {
    console.error('animatedReply error:', error);
    if (finalPayload) {
      return message.reply({ ...finalPayload, allowedMentions: { parse: [] } }).catch(() => null);
    }
    return null;
  }
}

function replySyntax(message, syntax, extra = '') {
  return message.reply({ embeds: [syntaxEmbed(syntax, extra)], allowedMentions: { parse: [] } });
}

function formatBucks(amount) {
  return Number(amount || 0).toLocaleString('en-US');
}

function clampBet(amount, balance) {
  const n = Math.floor(Number(amount));
  if (!Number.isFinite(n) || n <= 0) return null;
  if (n > balance) return 'NO_FUNDS';
  return n;
}

const activeBlackjackGames = new Map();
const activeTttGames = new Map();

function makeGameId(prefix) {
  return `${prefix}_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
}

function disableRows(rows) {
  return rows.map((row) => {
    const newRow = ActionRowBuilder.from(row);
    newRow.components.forEach((component) => component.setDisabled(true));
    return newRow;
  });
}

async function getTargetMemberFromArg(message, arg) {
  const id = parseUserId(arg);
  if (id) {
    try {
      return await message.guild.members.fetch(id);
    } catch {
      return null;
    }
  }
  return null;
}

async function getTargetMember(message, arg) {
  const mentioned = message.mentions.members.first();
  if (mentioned) return mentioned;

  const id = parseUserId(arg);
  if (!id) return null;

  try {
    return await message.guild.members.fetch(id);
  } catch {
    return null;
  }
}

function isTempVcChat(channel) {
  if (!channel || !channel.guild || channel.type !== ChannelType.GuildVoice) return false;
  const data = getGuildData(channel.guild.id);
  return Boolean(data.tempVcs[channel.id]);
}

function vcHelpEmbed() {
  return new EmbedBuilder()
    .setTitle('Voice Channel Controls')
    .setDescription([
      `Use these commands in any text channel with prefix \`${PREFIX}\`.`,
      '',
      `\`${PREFIX}vc lock\` - Lock your VC`,
      `\`${PREFIX}vc unlock\` - Unlock your VC`,
      `\`${PREFIX}vc hide\` - Hide your VC`,
      `\`${PREFIX}vc unhide\` - Unhide your VC`,
      `\`${PREFIX}vc permit @user\` - Let a user join`,
      `\`${PREFIX}vc reject @user\` - Remove/block a user`,
      `\`${PREFIX}vc transfer @user\` - Transfer ownership`,
      `\`${PREFIX}vc limit 5\` - Set user limit`,
      `\`${PREFIX}vc rename new name\` - Rename your VC`,
      `\`${PREFIX}vc bitrate 96\` - Set bitrate in kbps`,
      `\`${PREFIX}vc claim\` - Claim if owner left`,
      `\`${PREFIX}vc info\` - Show VC info`,
    ].join('\n'))
    .setFooter({ text: 'Users cannot chat inside created VC chats. Only the bot posts controls there.' });
}

// =========================
// UWUIFY
// =========================
const UWU_FACES = [
  'uwu', 'owo', 'UwU', 'OwO', '>w<', '^w^', '(≧◡≦)', '(｡♥‿♥｡)', '(つ✧ω✧)つ', '~', 'nya~', 'hehe~'
];

const UWU_REPLACEMENTS = [
  [/\br\b/gi, 'w'],
  [/\bl\b/gi, 'w'],
  [/r/gi, 'w'],
  [/l/gi, 'w'],
  [/ove/gi, 'uv'],
  [/you/gi, 'chu'],
  [/no/gi, 'nu'],
  [/the/gi, 'da'],
  [/this/gi, 'dis'],
  [/that/gi, 'dat'],
  [/what/gi, 'wut'],
  [/hello/gi, 'hewwo'],
  [/hi/gi, 'hai'],
  [/friend/gi, 'fwiend'],
  [/server/gi, 'sewvew'],
  [/really/gi, 'weawwy'],
  [/little/gi, 'wittle'],
  [/cute/gi, 'kawaii'],
  [/cool/gi, 'coow'],
  [/na/gi, 'nya'],
  [/ne/gi, 'nye'],
  [/ni/gi, 'nyi'],
  [/no/gi, 'nyo'],
  [/nu/gi, 'nyu'],
];

function maybeStutterWord(word) {
  if (!/^[a-zA-Z]{3,}$/.test(word)) return word;
  if (Math.random() > 0.22) return word;
  const first = word[0];
  return `${first}-${word}`;
}

function uwuifyText(input) {
  let text = String(input || '');
  if (!text.trim()) return text;

  for (const [from, to] of UWU_REPLACEMENTS) {
    text = text.replace(from, to);
  }

  text = text
    .split(/(\s+)/)
    .map((part) => (part.trim() ? maybeStutterWord(part) : part))
    .join('');

  text = text.replace(/[.!?]+/g, (m) => `${m}${Math.random() > 0.5 ? '!' : '~'}`);

  const face1 = UWU_FACES[Math.floor(Math.random() * UWU_FACES.length)];
  const face2 = UWU_FACES[Math.floor(Math.random() * UWU_FACES.length)];
  return `${text} ${face1} ${Math.random() > 0.6 ? face2 : ''}`.trim();
}

async function getOrCreateWebhook(channel) {
  if (!channel || typeof channel.fetchWebhooks !== 'function' || typeof channel.createWebhook !== 'function') {
    return null;
  }

  const me = channel.guild.members.me;
  const perms = channel.permissionsFor(me);
  if (!perms?.has(PermissionsBitField.Flags.ManageWebhooks)) return null;

  const hooks = await channel.fetchWebhooks();
  const existing = hooks.find((hook) => hook.owner?.id === client.user.id && hook.name === 'UwUify');
  if (existing) return existing;

  return channel.createWebhook({ name: 'UwUify' });
}

async function handleUwUMessage(message) {
  if (!message.guild || message.author.bot || !message.content) return;

  const data = getGuildData(message.guild.id);
  if (!data.uwuTargets.includes(message.author.id)) return;

  const me = message.guild.members.me;
  const perms = message.channel.permissionsFor(me);
  if (!perms?.has(PermissionsBitField.Flags.ManageMessages)) return;

  let webhook;
  try {
    webhook = await getOrCreateWebhook(message.channel);
  } catch (error) {
    console.error('Webhook error:', error);
    return;
  }

  if (!webhook) return;

  const uwuContent = uwuifyText(message.content);
  const attachmentUrls = [...message.attachments.values()].map((a) => a.url);
  const finalContent = [uwuContent, ...attachmentUrls].filter(Boolean).join('\n');

  try {
    await message.delete().catch(() => null);
    await webhook.send({
      content: finalContent.slice(0, 2000),
      username: message.member?.displayName || message.author.username,
      avatarURL: message.author.displayAvatarURL({ extension: 'png', size: 128 }),
      allowedMentions: { parse: [] },
    });
  } catch (error) {
    console.error('UwU send/delete error:', error);
  }
}

// =========================
// VOICEMASTER SETUP + TEMP VCS
// =========================
function botOverwrite(guild) {
  return {
    id: guild.members.me.id,
    allow: [
      PermissionsBitField.Flags.ViewChannel,
      PermissionsBitField.Flags.Connect,
      PermissionsBitField.Flags.SendMessages,
      PermissionsBitField.Flags.EmbedLinks,
      PermissionsBitField.Flags.ManageChannels,
      PermissionsBitField.Flags.MoveMembers,
      PermissionsBitField.Flags.ReadMessageHistory,
    ],
  };
}

function publicVcOverwrites(guild) {
  return [
    {
      id: guild.roles.everyone.id,
      allow: [PermissionsBitField.Flags.ViewChannel, PermissionsBitField.Flags.Connect],
      deny: [PermissionsBitField.Flags.SendMessages],
    },
    botOverwrite(guild),
  ];
}

function privateVcOverwrites(guild, ownerId) {
  return [
    {
      id: guild.roles.everyone.id,
      allow: [PermissionsBitField.Flags.ViewChannel],
      deny: [PermissionsBitField.Flags.Connect, PermissionsBitField.Flags.SendMessages],
    },
    {
      id: ownerId,
      allow: [PermissionsBitField.Flags.ViewChannel, PermissionsBitField.Flags.Connect],
      deny: [PermissionsBitField.Flags.SendMessages],
    },
    botOverwrite(guild),
  ];
}

async function setupVoiceMaster(message) {
  if (!hasManagerPerm(message.member)) {
    return message.reply('You need Manage Server or Manage Channels to run this.');
  }

  const guild = message.guild;
  const data = getGuildData(guild.id);

  const publicCategory = await guild.channels.create({
    name: 'Public Voice Channels',
    type: ChannelType.GuildCategory,
    reason: 'VoiceMaster setup',
  });

  const privateCategory = await guild.channels.create({
    name: 'Private Voice Channels',
    type: ChannelType.GuildCategory,
    reason: 'VoiceMaster setup',
  });

  const joinPublic = await guild.channels.create({
    name: 'Join Public VC',
    type: ChannelType.GuildVoice,
    parent: publicCategory.id,
    permissionOverwrites: publicVcOverwrites(guild),
    reason: 'VoiceMaster setup',
  });

  const randomPublic = await guild.channels.create({
    name: 'Random Public VC',
    type: ChannelType.GuildVoice,
    parent: publicCategory.id,
    permissionOverwrites: publicVcOverwrites(guild),
    reason: 'VoiceMaster setup',
  });

  const joinPrivate = await guild.channels.create({
    name: 'Join Private VC',
    type: ChannelType.GuildVoice,
    parent: privateCategory.id,
    permissionOverwrites: publicVcOverwrites(guild),
    reason: 'VoiceMaster setup',
  });

  data.vm = {
    publicCategoryId: publicCategory.id,
    privateCategoryId: privateCategory.id,
    joinPublicId: joinPublic.id,
    randomPublicId: randomPublic.id,
    joinPrivateId: joinPrivate.id,
  };
  saveDb();

  return message.reply('VoiceMaster setup complete. Join the created VC channels to test it.');
}

async function sendControlEmbed(channel, member) {
  try {
    await channel.send({
      content: `<@${member.id}>`,
      embeds: [vcHelpEmbed()],
      allowedMentions: { users: [member.id] },
    });
  } catch (error) {
    console.error('Could not send VC control embed:', error);
  }
}

async function createTempVc(member, type = 'public') {
  const guild = member.guild;
  const data = getGuildData(guild.id);
  const vm = data.vm;

  if (!vm) return null;

  const base = getVcUsername(member);
  const isPrivate = type === 'private';
  const parent = isPrivate ? vm.privateCategoryId : vm.publicCategoryId;
  const name = isPrivate ? `${base}'s Private VC` : `${base}'s Public VC`;

  const channel = await guild.channels.create({
    name,
    type: ChannelType.GuildVoice,
    parent,
    permissionOverwrites: isPrivate ? privateVcOverwrites(guild, member.id) : publicVcOverwrites(guild),
    reason: `Temporary ${type} VC created by VoiceMaster`,
  });

  data.tempVcs[channel.id] = {
    ownerId: member.id,
    ownerUsername: member.user.username,
    type,
    createdAt: Date.now(),
  };
  saveDb();

  try {
    await member.voice.setChannel(channel, 'VoiceMaster temporary VC created');
  } catch (error) {
    console.error('Could not move user to temp VC:', error);
  }

  await sendControlEmbed(channel, member);
  return channel;
}

async function cleanupTempVc(guild, channelId) {
  const data = getGuildData(guild.id);
  const record = data.tempVcs[channelId];
  if (!record) return;

  const channel = guild.channels.cache.get(channelId);
  if (!channel) {
    delete data.tempVcs[channelId];
    saveDb();
    return;
  }

  const realMembers = channel.members.filter((m) => !m.user.bot);
  if (realMembers.size > 0) return;

  delete data.tempVcs[channelId];
  saveDb();

  try {
    await channel.delete('Temporary VC empty');
  } catch (error) {
    console.error('Could not delete empty temp VC:', error);
  }
}

async function handleVoiceStateUpdate(oldState, newState) {
  const guild = newState.guild || oldState.guild;
  const data = getGuildData(guild.id);
  const vm = data.vm;

  if (newState.channelId && vm && !newState.member.user.bot) {
    if (newState.channelId === vm.joinPublicId) {
      await createTempVc(newState.member, 'public');
    } else if (newState.channelId === vm.joinPrivateId) {
      await createTempVc(newState.member, 'private');
    } else if (newState.channelId === vm.randomPublicId) {
      const publicTemps = Object.entries(data.tempVcs)
        .filter(([, rec]) => rec.type === 'public')
        .map(([id]) => guild.channels.cache.get(id))
        .filter((ch) => ch && ch.members.filter((m) => !m.user.bot).size > 0);

      if (publicTemps.length > 0) {
        const picked = publicTemps[Math.floor(Math.random() * publicTemps.length)];
        try {
          await newState.member.voice.setChannel(picked, 'VoiceMaster random public VC');
        } catch (error) {
          console.error('Could not move user to random public VC:', error);
        }
      } else {
        await createTempVc(newState.member, 'public');
      }
    }
  }

  if (oldState.channelId && data.tempVcs[oldState.channelId]) {
    setTimeout(() => cleanupTempVc(guild, oldState.channelId), TEMP_VC_DELETE_DELAY_MS);
  }
}

// =========================
// VC COMMANDS
// =========================
function getCurrentTempVc(message) {
  const voice = message.member.voice.channel;
  if (!voice) return { error: 'You need to be inside one of your temporary voice channels.' };

  const data = getGuildData(message.guild.id);
  const record = data.tempVcs[voice.id];
  if (!record) return { error: 'You are not inside a VoiceMaster temporary VC.' };

  return { channel: voice, record, data };
}

function isVcOwnerOrAdmin(message, record) {
  return record.ownerId === message.author.id || hasManagerPerm(message.member);
}

async function handleVcCommand(message, args) {
  const sub = (args.shift() || 'help').toLowerCase();

  if (sub === 'help') {
    return message.reply({ embeds: [vcHelpEmbed()] });
  }

  const current = getCurrentTempVc(message);
  if (current.error) return message.reply(current.error);

  const { channel, record, data } = current;

  if (sub === 'claim') {
    const ownerStillInside = channel.members.has(record.ownerId);
    if (ownerStillInside && !hasManagerPerm(message.member)) {
      return message.reply('The current owner is still in the VC, so you cannot claim it.');
    }

    record.ownerId = message.author.id;
    saveDb();
    return message.reply(`You are now the owner of ${channel}.`);
  }

  if (!isVcOwnerOrAdmin(message, record)) {
    return message.reply('Only the VC owner can use this command. Use `*vc claim` if the owner left.');
  }

  if (sub === 'lock') {
    await channel.permissionOverwrites.edit(message.guild.roles.everyone.id, {
      Connect: false,
      SendMessages: false,
    });
    return message.reply('Locked your VC.');
  }

  if (sub === 'unlock') {
    await channel.permissionOverwrites.edit(message.guild.roles.everyone.id, {
      ViewChannel: true,
      Connect: true,
      SendMessages: false,
    });
    return message.reply('Unlocked your VC.');
  }

  if (sub === 'hide') {
    await channel.permissionOverwrites.edit(message.guild.roles.everyone.id, {
      ViewChannel: false,
      SendMessages: false,
    });
    return message.reply('Hid your VC.');
  }

  if (sub === 'unhide') {
    await channel.permissionOverwrites.edit(message.guild.roles.everyone.id, {
      ViewChannel: true,
      SendMessages: false,
    });
    return message.reply('Unhid your VC.');
  }

  if (sub === 'permit') {
    const target = await getTargetMember(message, args[0]);
    if (!target) return replySyntax(message, `${PREFIX}vc permit @user`);

    await channel.permissionOverwrites.edit(target.id, {
      ViewChannel: true,
      Connect: true,
      SendMessages: false,
    });
    return message.reply(`Permitted ${target} to join your VC.`);
  }

  if (sub === 'reject') {
    const target = await getTargetMember(message, args[0]);
    if (!target) return replySyntax(message, `${PREFIX}vc reject @user`);

    await channel.permissionOverwrites.edit(target.id, {
      ViewChannel: false,
      Connect: false,
      SendMessages: false,
    });

    if (target.voice?.channelId === channel.id) {
      await target.voice.setChannel(null).catch(() => null);
    }

    return message.reply(`Rejected ${target} from your VC.`);
  }

  if (sub === 'transfer') {
    const target = await getTargetMember(message, args[0]);
    if (!target) return replySyntax(message, `${PREFIX}vc transfer @user`);
    if (target.user.bot) return message.reply('You cannot transfer ownership to a bot.');

    record.ownerId = target.id;
    data.tempVcs[channel.id] = record;
    saveDb();

    await channel.permissionOverwrites.edit(target.id, {
      ViewChannel: true,
      Connect: true,
      SendMessages: false,
    });

    return message.reply(`Transferred VC ownership to ${target}.`);
  }

  if (sub === 'limit') {
    const limit = Number.parseInt(args[0], 10);
    if (Number.isNaN(limit) || limit < 0 || limit > 99) {
      return replySyntax(message, `${PREFIX}vc limit 5`, 'Use a number from 0-99. Use 0 for no limit.');
    }

    await channel.setUserLimit(limit, 'VC owner changed user limit');
    return message.reply(limit === 0 ? 'Removed the user limit.' : `Set the user limit to ${limit}.`);
  }

  if (sub === 'rename') {
    const newName = args.join(' ').trim();
    if (!newName) return replySyntax(message, `${PREFIX}vc rename new name`);
    if (newName.length > 80) return message.reply('That name is too long. Keep it under 80 characters.');

    await channel.setName(newName, 'VC owner renamed channel');
    return message.reply(`Renamed your VC to **${newName}**.`);
  }

  if (sub === 'bitrate') {
    const kbps = Number.parseInt(args[0], 10);
    if (Number.isNaN(kbps) || kbps < 8) {
      return replySyntax(message, `${PREFIX}vc bitrate 96`);
    }

    const max = message.guild.maximumBitrate || 96000;
    const final = Math.min(kbps * 1000, max);
    await channel.setBitrate(final, 'VC owner changed bitrate');
    return message.reply(`Set bitrate to ${Math.round(final / 1000)} kbps.`);
  }

  if (sub === 'info') {
    const owner = await message.guild.members.fetch(record.ownerId).catch(() => null);
    const embed = new EmbedBuilder()
      .setTitle('VC Info')
      .addFields(
        { name: 'Channel', value: `${channel}`, inline: true },
        { name: 'Owner', value: owner ? `${owner}` : `<@${record.ownerId}>`, inline: true },
        { name: 'Type', value: record.type || 'public', inline: true },
        { name: 'Members', value: `${channel.members.filter((m) => !m.user.bot).size}`, inline: true },
        { name: 'Limit', value: channel.userLimit ? `${channel.userLimit}` : 'None', inline: true },
      );

    return message.reply({ embeds: [embed] });
  }

  return replySyntax(message, `${PREFIX}vc help`, 'Unknown VC subcommand.');
}


// =========================
// ECONOMY + GAMBLING
// =========================
const STARTING_BALANCE = Number(process.env.STARTING_BALANCE || 500);
const DAILY_REWARD = Number(process.env.DAILY_REWARD || 2500);
const WORK_MIN = Number(process.env.WORK_MIN || 250);
const WORK_MAX = Number(process.env.WORK_MAX || 1200);
const BEG_MIN = Number(process.env.BEG_MIN || 25);
const BEG_MAX = Number(process.env.BEG_MAX || 350);
const DAILY_COOLDOWN_MS = 24 * 60 * 60 * 1000;
const WORK_COOLDOWN_MS = Number(process.env.WORK_COOLDOWN_MS || 15 * 60 * 1000);
const BEG_COOLDOWN_MS = Number(process.env.BEG_COOLDOWN_MS || 5 * 60 * 1000);
const DONATE_DAILY_LIMIT = Number(process.env.DONATE_DAILY_LIMIT || 250000);
const MAX_BET = Number(process.env.MAX_BET || 150000);

function getEcoUser(guildId, userId) {
  const data = getGuildData(guildId);
  if (!data.economy.users[userId]) {
    data.economy.users[userId] = {
      balance: STARTING_BALANCE,
      totalEarned: STARTING_BALANCE,
      totalLost: 0,
      dailyStreak: 0,
      lastDaily: 0,
      lastWork: 0,
      lastBeg: 0,
      donatedToday: 0,
      donateWindowStart: 0,
      wins: 0,
      losses: 0,
    };
    saveDb();
  }
  return data.economy.users[userId];
}

function addBalance(guildId, userId, amount) {
  const user = getEcoUser(guildId, userId);
  user.balance = Math.max(0, Math.floor((user.balance || 0) + amount));
  if (amount > 0) user.totalEarned = Math.floor((user.totalEarned || 0) + amount);
  if (amount < 0) user.totalLost = Math.floor((user.totalLost || 0) + Math.abs(amount));
  saveDb();
  return user;
}

function timeLeft(ms) {
  const total = Math.max(0, Math.ceil(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function economyHelpEmbed() {
  return new EmbedBuilder()
    .setColor(0xff8a00)
    .setTitle('💸 Smoke Bucks Economy Help')
    .setDescription([
      'Welcome to the **Smoke Bucks** economy. Earn, gamble, donate, and climb the leaderboard.',
      '',
      '✨ **Main Commands**',
      `> \`${PREFIX}balance\` / \`${PREFIX}bal\` — Check your balance`,
      `> \`${PREFIX}balance @user\` — Check another user`,
      `> \`${PREFIX}daily\` — Claim your daily reward`,
      `> \`${PREFIX}work\` — Work for Smoke Bucks`,
      `> \`${PREFIX}beg\` — Beg for a small random amount`,
      `> \`${PREFIX}donate @user amount\` / \`${PREFIX}pay @user amount\` — Donate bucks`,
      `> \`${PREFIX}leaderboard\` / \`${PREFIX}lb\` — View top balances`,
      `> \`${PREFIX}quests\` / \`${PREFIX}missions\` — View your daily quests`,
      '',
      '🎰 **Games & Gambling**',
      `> \`${PREFIX}coinflip amount heads/tails\` — Animated 50/50 flip`,
      `> \`${PREFIX}slots amount\` — Premium slot machine spin`,
      `> \`${PREFIX}dice amount over/under\` — Animated dice roll`,
      `> \`${PREFIX}roulette amount red/black/green\` — Roulette wheel spin`,
      `> \`${PREFIX}blackjack amount\` — Interactive Hit/Stand blackjack`,
      `> \`${PREFIX}ttt @user [amount]\` — Button Tic-Tac-Toe challenge`,
      '',
      '🔒 **Limits**',
      `> Max bet: **${formatBucks(MAX_BET)}** Smoke Bucks`,
      `> Daily donate limit: **${formatBucks(DONATE_DAILY_LIMIT)}** Smoke Bucks`,
    ].join('\n'))
    .setFooter({ text: 'Tip: amount can be a number, half, or all.' })
    .setTimestamp();
}

function parseAmountArg(arg, balance) {
  if (!arg) return null;
  const lower = String(arg).toLowerCase();
  if (lower === 'all') return Math.min(balance, MAX_BET);
  if (lower === 'half') return Math.min(Math.floor(balance / 2), MAX_BET);
  const cleaned = lower.replace(/,/g, '');
  const n = Math.floor(Number(cleaned));
  if (!Number.isFinite(n) || n <= 0) return null;
  return Math.min(n, MAX_BET);
}


// =========================
// DAILY QUESTS / MISSIONS
// =========================
const QUEST_RESET_TZ = process.env.QUEST_RESET_TZ || 'America/Los_Angeles';
const REGULAR_QUESTS_PER_DAY = Number(process.env.REGULAR_QUESTS_PER_DAY || 3);
const BOOSTER_QUESTS_PER_DAY = Number(process.env.BOOSTER_QUESTS_PER_DAY || 5);
const activeVcQuestSessions = new Map();

function todayKey() {
  const formatter = new Intl.DateTimeFormat('en-CA', { timeZone: QUEST_RESET_TZ, year: 'numeric', month: '2-digit', day: '2-digit' });
  return formatter.format(new Date());
}

const QUEST_POOL = [
  { id: 'chat_25', type: 'chat', goal: 25, reward: 600, name: 'Chat 25 messages', desc: 'Send 25 messages in the server.' },
  { id: 'chat_50', type: 'chat', goal: 50, reward: 1200, name: 'Chat 50 messages', desc: 'Send 50 messages in the server.' },
  { id: 'chat_100', type: 'chat', goal: 100, reward: 2800, name: 'Chat 100 messages', desc: 'Send 100 messages in the server.' },
  { id: 'vc_10', type: 'vc_minutes', goal: 10, reward: 900, name: 'VC for 10 minutes', desc: 'Stay active in a voice channel for 10 minutes.' },
  { id: 'vc_20', type: 'vc_minutes', goal: 20, reward: 1800, name: 'VC for 20 minutes', desc: 'Stay active in a voice channel for 20 minutes.' },
  { id: 'vc_45', type: 'vc_minutes', goal: 45, reward: 3500, name: 'VC for 45 minutes', desc: 'Stay active in a voice channel for 45 minutes.' },
  { id: 'ttt_play_1', type: 'play_ttt', goal: 1, reward: 700, name: 'Play Tic-Tac-Toe', desc: 'Play 1 Tic-Tac-Toe game.' },
  { id: 'ttt_play_3', type: 'play_ttt', goal: 3, reward: 2200, name: 'Play Tic-Tac-Toe 3 times', desc: 'Play 3 Tic-Tac-Toe games.' },
  { id: 'ttt_win_1', type: 'win_ttt', goal: 1, reward: 2000, name: 'Win Tic-Tac-Toe', desc: 'Win 1 Tic-Tac-Toe game.' },
  { id: 'bj_play_2', type: 'play_blackjack', goal: 2, reward: 1000, name: 'Play Blackjack 2 times', desc: 'Play 2 blackjack games.' },
  { id: 'bj_play_5', type: 'play_blackjack', goal: 5, reward: 2600, name: 'Play Blackjack 5 times', desc: 'Play 5 blackjack games.' },
  { id: 'slots_play_3', type: 'play_slots', goal: 3, reward: 1200, name: 'Spin Slots 3 times', desc: 'Play slots 3 times.' },
  { id: 'slots_play_5', type: 'play_slots', goal: 5, reward: 2200, name: 'Spin Slots 5 times', desc: 'Play slots 5 times.' },
  { id: 'coinflip_win_1', type: 'win_coinflip', goal: 1, reward: 1800, name: 'Win a Coinflip', desc: 'Win 1 coinflip.' },
  { id: 'roulette_win_1', type: 'win_roulette', goal: 1, reward: 2500, name: 'Win Roulette', desc: 'Win 1 roulette game.' },
  { id: 'dice_play_3', type: 'play_dice', goal: 3, reward: 1100, name: 'Roll Dice 3 times', desc: 'Play dice 3 times.' },
  { id: 'gamble_win_2', type: 'win_gamble', goal: 2, reward: 2600, name: 'Win 2 Gambling Games', desc: 'Win any 2 gambling games.' },
  { id: 'daily_use', type: 'use_daily', goal: 1, reward: 750, name: 'Claim Daily', desc: 'Use your daily reward command.' },
  { id: 'work_2', type: 'use_work', goal: 2, reward: 1200, name: 'Work 2 times', desc: 'Use the work command 2 times.' },
  { id: 'work_3', type: 'use_work', goal: 3, reward: 2200, name: 'Work 3 times', desc: 'Use the work command 3 times.' },
  { id: 'donate_1', type: 'donate', goal: 1, reward: 1500, name: 'Donate Smoke Bucks', desc: 'Donate Smoke Bucks to another member once.' },
  { id: 'wager_2500', type: 'wager', goal: 2500, reward: 2000, name: 'Wager 2,500 Smoke Bucks', desc: 'Wager 2,500 total Smoke Bucks in games.' },
  { id: 'wager_10000', type: 'wager', goal: 10000, reward: 5500, name: 'Wager 10,000 Smoke Bucks', desc: 'Wager 10,000 total Smoke Bucks in games.' },
];

function shuffleSeeded(items, seedText) {
  const arr = [...items];
  let seed = 0;
  for (const ch of String(seedText)) seed = ((seed << 5) - seed + ch.charCodeAt(0)) >>> 0;
  for (let i = arr.length - 1; i > 0; i--) {
    seed = (seed * 1664525 + 1013904223) >>> 0;
    const j = seed % (i + 1);
    [arr[i], arr[j]] = [arr[j], arr[i]];
  }
  return arr;
}

function ensureQuestStore(data) {
  if (!data.economy.quests) data.economy.quests = { users: {} };
  if (!data.economy.quests.users) data.economy.quests.users = {};
  return data.economy.quests;
}

function getQuestUser(guildId, userId) {
  const data = getGuildData(guildId);
  const store = ensureQuestStore(data);
  if (!store.users[userId]) store.users[userId] = { date: '', quests: [], lastTextChannelId: null, vcProgressMs: 0 };
  return store.users[userId];
}

function isBooster(member) {
  return Boolean(member?.premiumSince || member?.roles?.premiumSubscriberRole);
}

function questCountFor(member) {
  return isBooster(member) ? BOOSTER_QUESTS_PER_DAY : REGULAR_QUESTS_PER_DAY;
}

function assignDailyQuests(member) {
  const guildId = member.guild.id;
  const qUser = getQuestUser(guildId, member.id);
  const key = todayKey();
  const count = questCountFor(member);
  if (qUser.date === key && Array.isArray(qUser.quests) && qUser.quests.length >= count) return qUser.quests.slice(0, count);

  const selected = [];
  const usedTypes = new Set();
  const shuffled = shuffleSeeded(QUEST_POOL, `${guildId}:${member.id}:${key}:${qUser.quests?.map((q) => q.id).join(',') || ''}`);
  for (const quest of shuffled) {
    if (selected.length >= count) break;
    if (usedTypes.has(quest.type)) continue;
    selected.push({ ...quest, progress: 0, completed: false, paid: false, completedAt: 0 });
    usedTypes.add(quest.type);
  }
  for (const quest of shuffled) {
    if (selected.length >= count) break;
    if (!selected.some((q) => q.id === quest.id)) selected.push({ ...quest, progress: 0, completed: false, paid: false, completedAt: 0 });
  }
  qUser.date = key;
  qUser.quests = selected;
  saveDb();
  return qUser.quests;
}

function questProgressBar(done, goal) {
  const total = 10;
  const filled = Math.min(total, Math.floor((Number(done || 0) / Math.max(1, Number(goal || 1))) * total));
  return '▰'.repeat(filled) + '▱'.repeat(total - filled);
}

function questsEmbed(member) {
  const quests = assignDailyQuests(member);
  const lines = quests.map((q, i) => {
    const progress = Math.min(q.progress || 0, q.goal || 1);
    const status = q.completed ? '✅' : '✨';
    return [
      `${status} **${i + 1}. ${q.name}**`,
      `> ${q.desc}`,
      `> ${questProgressBar(progress, q.goal)} **${formatBucks(progress)}/${formatBucks(q.goal)}** • Reward: **${formatBucks(q.reward)}**`,
    ].join('\n');
  });
  return new EmbedBuilder()
    .setColor(0xff8a00)
    .setTitle('📜 Daily Smoke Quests')
    .setDescription([
      `${member} here are your missions for **${todayKey()}**.`,
      isBooster(member) ? '🚀 Booster bonus active: **5 quests today**.' : 'Members get **3 quests daily**. Boosters get **5**.',
      '',
      ...lines,
    ].join('\n'))
    .setFooter({ text: 'Rewards are paid automatically as soon as a quest is completed.' })
    .setTimestamp();
}

async function notifyQuestComplete(guild, userId, quest) {
  const qUser = getQuestUser(guild.id, userId);
  const channel = qUser.lastTextChannelId ? guild.channels.cache.get(qUser.lastTextChannelId) : null;
  if (!channel || !channel.isTextBased?.()) return;
  await channel.send({
    content: `<@${userId}>`,
    embeds: [premiumEmbed('✅ Mission Complete', `You completed **${quest.name}** and earned **${formatBucks(quest.reward)} Smoke Bucks**.`)],
    allowedMentions: { users: [userId] },
  }).catch(() => null);
}

async function progressQuest(guild, memberOrUserId, type, amount = 1, channelId = null) {
  if (!guild || !type || !amount) return;
  const member = typeof memberOrUserId === 'string'
    ? await guild.members.fetch(memberOrUserId).catch(() => null)
    : memberOrUserId;
  const userId = typeof memberOrUserId === 'string' ? memberOrUserId : memberOrUserId?.id;
  if (!userId || !member || member.user?.bot) return;

  const qUser = getQuestUser(guild.id, userId);
  if (channelId) qUser.lastTextChannelId = channelId;
  const quests = assignDailyQuests(member);
  let changed = false;
  for (const quest of quests) {
    if (quest.completed || quest.type !== type) continue;
    quest.progress = Math.min(quest.goal, (quest.progress || 0) + amount);
    changed = true;
    if (quest.progress >= quest.goal) {
      quest.completed = true;
      quest.paid = true;
      quest.completedAt = Date.now();
      addBalance(guild.id, userId, quest.reward);
      await notifyQuestComplete(guild, userId, quest);
    }
  }
  if (changed) saveDb();
}

function rememberLastTextChannel(message) {
  const qUser = getQuestUser(message.guild.id, message.author.id);
  qUser.lastTextChannelId = message.channel.id;
  saveDb();
}

async function tickVcQuests() {
  for (const guild of client.guilds.cache.values()) {
    for (const channel of guild.channels.cache.values()) {
      if (!channel || channel.type !== ChannelType.GuildVoice) continue;
      for (const member of channel.members.values()) {
        if (member.user.bot) continue;
        const key = `${guild.id}:${member.id}`;
        const session = activeVcQuestSessions.get(key) || { lastTick: Date.now() };
        const now = Date.now();
        const diff = Math.max(0, now - (session.lastTick || now));
        session.lastTick = now;
        activeVcQuestSessions.set(key, session);
        if (diff > 0) await progressQuest(guild, member, 'vc_minutes', diff / 60000);
      }
    }
  }
}

function betOrSyntax(message, amountArg, syntax) {
  const user = getEcoUser(message.guild.id, message.author.id);
  const bet = parseAmountArg(amountArg, user.balance);
  if (!bet) return { error: () => replySyntax(message, syntax, 'Amount can be a number, `half`, or `all`.') };
  if (bet > user.balance) {
    return { error: () => message.reply({ embeds: [economyErrorEmbed('Not Enough Smoke Bucks', `You only have **${formatBucks(user.balance)}** Smoke Bucks.`)] }) };
  }
  return { user, bet };
}

function randInt(min, max) {
  return Math.floor(Math.random() * (max - min + 1)) + min;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function drawCard() {
  const value = randInt(1, 13);
  if (value === 1) return { label: 'A', value: 11 };
  if (value >= 11) return { label: ['J', 'Q', 'K'][value - 11], value: 10 };
  return { label: String(value), value };
}

function handValue(hand) {
  let total = hand.reduce((sum, card) => sum + card.value, 0);
  let aces = hand.filter((card) => card.label === 'A').length;
  while (total > 21 && aces > 0) {
    total -= 10;
    aces -= 1;
  }
  return total;
}

function handText(hand) {
  return hand.map((card) => card.label).join(', ');
}

function blackjackRows(gameId, disabled = false) {
  return [
    new ActionRowBuilder().addComponents(
      new ButtonBuilder()
        .setCustomId(`${gameId}:hit`)
        .setLabel('🃏 Hit')
        .setStyle(ButtonStyle.Primary)
        .setDisabled(disabled),
      new ButtonBuilder()
        .setCustomId(`${gameId}:stand`)
        .setLabel('🛑 Stand')
        .setStyle(ButtonStyle.Secondary)
        .setDisabled(disabled),
    ),
  ];
}

function blackjackEmbed(game, finished = false) {
  const playerValue = handValue(game.player);
  const dealerValue = finished ? handValue(game.dealer) : handValue([game.dealer[0]]);
  const status = finished ? game.resultText : 'Choose **🃏 Hit** or **🛑 Stand** to play your hand.';
  return new EmbedBuilder()
    .setColor(finished ? 0xff8a00 : 0x5865f2)
    .setTitle(finished ? '♠️ Blackjack Results' : '♠️ Premium Blackjack')
    .setDescription([
      `👤 **Player:** <@${game.userId}>`,
      `💸 **Bet:** ${formatBucks(game.bet)} Smoke Bucks`,
      '',
      `🃏 **Your Hand**`,
      `> ${handText(game.player)}  •  **${playerValue}**`,
      '',
      `🏦 **Dealer Hand**`,
      finished
        ? `> ${handText(game.dealer)}  •  **${dealerValue}**`
        : `> ${game.dealer[0].label}, ❔ Hidden`,
      '',
      `✨ ${status}`,
    ].join('\n'))
    .setFooter({ text: 'Smoke Bucks Casino' })
    .setTimestamp();
}

function finishBlackjackGame(guildId, game, outcome) {
  let resultText;
  const eco = getEcoUser(guildId, game.userId);

  if (outcome === 'win') {
    addBalance(guildId, game.userId, game.bet * 2);
    eco.wins = (eco.wins || 0) + 1;
    resultText = `You won **${formatBucks(game.bet)}** Smoke Bucks.`;
  } else if (outcome === 'push') {
    addBalance(guildId, game.userId, game.bet);
    resultText = 'Push. Your bet was returned.';
  } else {
    eco.losses = (eco.losses || 0) + 1;
    resultText = `You lost **${formatBucks(game.bet)}** Smoke Bucks.`;
  }

  saveDb();
  game.resultText = resultText;
  activeBlackjackGames.delete(`${guildId}:${game.userId}`);
}

async function startInteractiveBlackjack(message, command, args) {
  const parsed = betOrSyntax(message, args[0], `${PREFIX}${command} amount`);
  if (parsed.error) return parsed.error();

  const key = `${message.guild.id}:${message.author.id}`;
  if (activeBlackjackGames.has(key)) {
    return message.reply({ embeds: [economyErrorEmbed('Game Already Running', 'Finish your current blackjack game first.')] });
  }

  addBalance(message.guild.id, message.author.id, -parsed.bet);
  await progressQuest(message.guild, message.member, 'play_blackjack', 1, message.channel.id);
  await progressQuest(message.guild, message.member, 'wager', parsed.bet, message.channel.id);

  const gameId = makeGameId('bj');
  const game = {
    gameId,
    userId: message.author.id,
    bet: parsed.bet,
    player: [drawCard(), drawCard()],
    dealer: [drawCard(), drawCard()],
    resultText: '',
  };

  activeBlackjackGames.set(key, game);

  const rows = blackjackRows(gameId);
  const dealFrames = [
    premiumEmbed('♠️ Premium Blackjack', ['Opening the table...', '', '```', 'Dealer is shuffling the deck...', '```'].join('\n'), 0x5865f2),
    premiumEmbed('♠️ Premium Blackjack', ['Cards are being dealt...', '', '```', 'Player: 🂠 🂠', 'Dealer: 🂠 🂠', '```'].join('\n'), 0x5865f2),
    premiumEmbed('♠️ Premium Blackjack', ['Revealing your hand...', '', `Player: **${handText(game.player)}**`, 'Dealer: one card hidden...'], 0x5865f2),
  ];
  const gameMessage = await message.reply({ embeds: [dealFrames[0]], components: [], allowedMentions: { parse: [] } });
  for (const frame of dealFrames.slice(1)) {
    await sleep(900);
    await gameMessage.edit({ embeds: [frame], components: [], allowedMentions: { parse: [] } }).catch(() => null);
  }
  await sleep(900);
  await gameMessage.edit({ embeds: [blackjackEmbed(game)], components: rows, allowedMentions: { parse: [] } }).catch(() => null);

  const finishAndEdit = async (outcome) => {
    finishBlackjackGame(message.guild.id, game, outcome);
    await gameMessage.edit({ embeds: [blackjackEmbed(game, true)], components: blackjackRows(gameId, true) }).catch(() => null);
  };

  if (handValue(game.player) === 21) {
    while (handValue(game.dealer) < 17) game.dealer.push(drawCard());
    const dv = handValue(game.dealer);
    const outcome = dv === 21 ? 'push' : 'win';
    return finishAndEdit(outcome);
  }

  const collector = gameMessage.createMessageComponentCollector({ time: 90_000 });

  collector.on('collect', async (interaction) => {
    if (!interaction.customId.startsWith(`${gameId}:`)) return;
    if (interaction.user.id !== game.userId) {
      return interaction.reply({ content: 'This is not your blackjack game.', ephemeral: true });
    }

    const action = interaction.customId.split(':')[1];

    if (action === 'hit') {
      game.player.push(drawCard());
      const pv = handValue(game.player);
      if (pv > 21) {
        await interaction.update({ embeds: [blackjackEmbed(game, true)], components: blackjackRows(gameId, true) });
        finishBlackjackGame(message.guild.id, game, 'lose');
        await gameMessage.edit({ embeds: [blackjackEmbed(game, true)], components: blackjackRows(gameId, true) }).catch(() => null);
        collector.stop('finished');
        return;
      }
      return interaction.update({ embeds: [blackjackEmbed(game)], components: blackjackRows(gameId) });
    }

    if (action === 'stand') {
      while (handValue(game.dealer) < 17) game.dealer.push(drawCard());
      const pv = handValue(game.player);
      const dv = handValue(game.dealer);
      let outcome = 'lose';
      if (dv > 21 || pv > dv) outcome = 'win';
      else if (pv === dv) outcome = 'push';
      finishBlackjackGame(message.guild.id, game, outcome);
      await interaction.update({ embeds: [blackjackEmbed(game, true)], components: blackjackRows(gameId, true) });
      collector.stop('finished');
    }
  });

  collector.on('end', async (_collected, reason) => {
    if (reason === 'finished') return;
    if (!activeBlackjackGames.has(key)) return;
    finishBlackjackGame(message.guild.id, game, 'lose');
    game.resultText = `You took too long and lost **${formatBucks(game.bet)}** Smoke Bucks.`;
    await gameMessage.edit({ embeds: [blackjackEmbed(game, true)], components: blackjackRows(gameId, true) }).catch(() => null);
  });
}

function tttRows(gameId, board, disabled = false) {
  const rows = [];
  for (let row = 0; row < 3; row++) {
    const actionRow = new ActionRowBuilder();
    for (let col = 0; col < 3; col++) {
      const i = row * 3 + col;
      const mark = board[i];
      actionRow.addComponents(
        new ButtonBuilder()
          .setCustomId(`${gameId}:move:${i}`)
          .setLabel(mark === 'X' ? '✕' : mark === 'O' ? '○' : '·')
          .setStyle(mark === 'X' ? ButtonStyle.Danger : mark === 'O' ? ButtonStyle.Primary : ButtonStyle.Secondary)
          .setDisabled(disabled || Boolean(mark)),
      );
    }
    rows.push(actionRow);
  }
  return rows;
}

function tttWinner(board) {
  const wins = [
    [0, 1, 2], [3, 4, 5], [6, 7, 8],
    [0, 3, 6], [1, 4, 7], [2, 5, 8],
    [0, 4, 8], [2, 4, 6],
  ];
  for (const [a, b, c] of wins) {
    if (board[a] && board[a] === board[b] && board[a] === board[c]) return board[a];
  }
  if (board.every(Boolean)) return 'tie';
  return null;
}

function tttCell(cell) {
  if (cell === 'X') return '❌';
  if (cell === 'O') return '⭕';
  return '⬛';
}

function tttBoardText(board) {
  const cells = board.map(tttCell);
  return `${cells[0]} ${cells[1]} ${cells[2]}\n${cells[3]} ${cells[4]} ${cells[5]}\n${cells[6]} ${cells[7]} ${cells[8]}`;
}

function tttEmbed(game, finishedText = '') {
  const currentId = game.turn === 'X' ? game.xId : game.oId;
  const isFinished = Boolean(finishedText);
  const status = isFinished ? finishedText : `**Turn:** <@${currentId}>`;
  return new EmbedBuilder()
    .setColor(isFinished ? 0xffb000 : 0x2f3136)
    .setAuthor({ name: 'Tic Tac Toe', iconURL: 'https://cdn.discordapp.com/embed/avatars/0.png' })
    .setDescription([
      `❌ <@${game.xId}> **vs** ⭕ <@${game.oId}>`,
      `💸 **${formatBucks(game.bet)} Smoke Bucks** each`,
      '',
      tttBoardText(game.board),
      '',
      `◜ ${status}`,
    ].join('\n'))
    .setFooter({ text: isFinished ? 'Game ended' : 'Click a tile below to play your move' })
    .setTimestamp();
}

async function startTicTacToe(message, command, args) {
  const target = await getTargetMember(message, args[0]);
  if (!target) return replySyntax(message, `${PREFIX}${command} @user [amount]`, 'Example: `*ttt @user 500`');
  if (target.id === message.author.id) return message.reply({ embeds: [economyErrorEmbed('Invalid Game', 'You cannot play Tic-Tac-Toe against yourself.')] });
  if (target.user.bot) return message.reply({ embeds: [economyErrorEmbed('Invalid Game', 'You cannot play Tic-Tac-Toe against a bot.')] });

  const challengerEco = getEcoUser(message.guild.id, message.author.id);
  const requestedBet = args[1] ? parseAmountArg(args[1], challengerEco.balance) : 250;
  const bet = requestedBet || 250;
  if (bet > MAX_BET) return replySyntax(message, `${PREFIX}${command} @user [amount]`, `Max bet is **${formatBucks(MAX_BET)}** Smoke Bucks.`);

  const targetEco = getEcoUser(message.guild.id, target.id);
  if (challengerEco.balance < bet) return message.reply({ embeds: [economyErrorEmbed('Not Enough Smoke Bucks', `You only have **${formatBucks(challengerEco.balance)}** Smoke Bucks.`)] });
  if (targetEco.balance < bet) return message.reply({ embeds: [economyErrorEmbed('Opponent Cannot Afford Bet', `${target} only has **${formatBucks(targetEco.balance)}** Smoke Bucks.`)] });

  const ids = [message.author.id, target.id].sort().join(':');
  const activeKey = `${message.guild.id}:${ids}`;
  if (activeTttGames.has(activeKey)) return message.reply({ embeds: [economyErrorEmbed('Game Already Running', 'One of you already has a Tic-Tac-Toe game running.')] });

  const confirmId = makeGameId('ttt_confirm');
  const confirmRows = [
    new ActionRowBuilder().addComponents(
      new ButtonBuilder().setCustomId(`${confirmId}:accept`).setLabel('✅ Accept').setStyle(ButtonStyle.Success),
      new ButtonBuilder().setCustomId(`${confirmId}:deny`).setLabel('❌ Deny').setStyle(ButtonStyle.Danger),
    ),
  ];

  const confirmEmbed = new EmbedBuilder()
    .setTitle('Tic-Tac-Toe Challenge')
    .setDescription([
      `${message.author} challenged ${target} to Tic-Tac-Toe.`,
      `Bet: **${formatBucks(bet)}** Smoke Bucks each`,
      '',
      `${target}, do you accept?`,
    ].join('\n'));

  const challengeMessage = await message.reply({ content: `${target}`, embeds: [confirmEmbed], components: confirmRows, allowedMentions: { users: [target.id] } });

  const confirmCollector = challengeMessage.createMessageComponentCollector({ time: 60_000 });

  confirmCollector.on('collect', async (interaction) => {
    if (!interaction.customId.startsWith(`${confirmId}:`)) return;
    if (interaction.user.id !== target.id) {
      return interaction.reply({ content: 'Only the challenged user can respond to this.', ephemeral: true });
    }

    const action = interaction.customId.split(':')[1];
    if (action === 'deny') {
      confirmCollector.stop('denied');
      await interaction.update({ content: `<@${message.author.id}> your game got denied by <@${target.id}>.`, embeds: [], components: [], allowedMentions: { users: [message.author.id, target.id] } });
      return;
    }

    // Recheck balances when accepted so nobody can dodge the bet after the challenge is sent.
    const freshChallenger = getEcoUser(message.guild.id, message.author.id);
    const freshTarget = getEcoUser(message.guild.id, target.id);
    if (freshChallenger.balance < bet || freshTarget.balance < bet) {
      confirmCollector.stop('no_funds');
      await interaction.update({ content: 'Tic-Tac-Toe canceled because one player no longer has enough Smoke Bucks.', embeds: [], components: [] });
      return;
    }

    addBalance(message.guild.id, message.author.id, -bet);
    addBalance(message.guild.id, target.id, -bet);

    const gameId = makeGameId('ttt');
    const game = {
      gameId,
      xId: message.author.id,
      oId: target.id,
      bet,
      board: Array(9).fill(null),
      turn: 'X',
      activeKey,
    };
    activeTttGames.set(activeKey, game);
    await progressQuest(message.guild, message.member, 'play_ttt', 1, message.channel.id);
    await progressQuest(message.guild, target, 'play_ttt', 1, message.channel.id);
    await progressQuest(message.guild, message.member, 'wager', bet, message.channel.id);
    await progressQuest(message.guild, target, 'wager', bet, message.channel.id);
    confirmCollector.stop('accepted');

    await interaction.update({ content: '', embeds: [tttEmbed(game)], components: tttRows(gameId, game.board), allowedMentions: { parse: [] } });

    const gameCollector = challengeMessage.createMessageComponentCollector({ time: 180_000 });
    gameCollector.on('collect', async (moveInteraction) => {
      if (!moveInteraction.customId.startsWith(`${gameId}:move:`)) return;
      const currentId = game.turn === 'X' ? game.xId : game.oId;
      if (moveInteraction.user.id !== currentId) {
        return moveInteraction.reply({ content: 'It is not your turn.', ephemeral: true });
      }
      const index = Number(moveInteraction.customId.split(':')[2]);
      if (!Number.isInteger(index) || index < 0 || index > 8 || game.board[index]) {
        return moveInteraction.reply({ content: 'That spot is not available.', ephemeral: true });
      }

      game.board[index] = game.turn;
      const winner = tttWinner(game.board);

      if (winner) {
        activeTttGames.delete(activeKey);
        let finishedText;
        if (winner === 'tie') {
          addBalance(message.guild.id, game.xId, bet);
          addBalance(message.guild.id, game.oId, bet);
          finishedText = 'Tie game. Both bets were returned.';
        } else {
          const winnerId = winner === 'X' ? game.xId : game.oId;
          const loserId = winner === 'X' ? game.oId : game.xId;
          addBalance(message.guild.id, winnerId, bet * 2);
          const winnerEco = getEcoUser(message.guild.id, winnerId);
          const loserEco = getEcoUser(message.guild.id, loserId);
          winnerEco.wins = (winnerEco.wins || 0) + 1;
          loserEco.losses = (loserEco.losses || 0) + 1;
          saveDb();
          await progressQuest(message.guild, winnerId, 'win_ttt', 1, message.channel.id);
          await progressQuest(message.guild, winnerId, 'win_gamble', 1, message.channel.id);
          finishedText = `<@${winnerId}> won **${formatBucks(bet)}** Smoke Bucks from <@${loserId}>.`;
        }
        await moveInteraction.update({ embeds: [tttEmbed(game, finishedText)], components: tttRows(gameId, game.board, true), allowedMentions: { parse: [] } });
        gameCollector.stop('finished');
        return;
      }

      game.turn = game.turn === 'X' ? 'O' : 'X';
      await moveInteraction.update({ embeds: [tttEmbed(game)], components: tttRows(gameId, game.board), allowedMentions: { parse: [] } });
    });

    gameCollector.on('end', async (_collected, reason) => {
      if (reason === 'finished') return;
      if (!activeTttGames.has(activeKey)) return;
      activeTttGames.delete(activeKey);
      addBalance(message.guild.id, game.xId, bet);
      addBalance(message.guild.id, game.oId, bet);
      await challengeMessage.edit({ embeds: [tttEmbed(game, 'Game expired. Both bets were returned.')], components: tttRows(gameId, game.board, true), allowedMentions: { parse: [] } }).catch(() => null);
    });
  });

  confirmCollector.on('end', async (_collected, reason) => {
    if (reason === 'accepted' || reason === 'denied' || reason === 'no_funds') return;
    await challengeMessage.edit({ content: `<@${message.author.id}> your Tic-Tac-Toe challenge expired.`, embeds: [], components: [], allowedMentions: { users: [message.author.id] } }).catch(() => null);
  });
}

async function handleEconomyCommand(message, command, args) {
  if (command === 'economy' || command === 'eco') {
    const sub = (args[0] || 'help').toLowerCase();
    if (sub !== 'help') return replySyntax(message, `${PREFIX}${command} help`);
    return message.reply({ embeds: [economyHelpEmbed()] });
  }


  if (command === 'quests' || command === 'missions' || command === 'dailyquests') {
    return message.reply({ embeds: [questsEmbed(message.member)], allowedMentions: { parse: [] } });
  }

  if (command === 'balance' || command === 'bal') {
    let target = message.member;
    if (args[0]) {
      const found = await getTargetMember(message, args[0]);
      if (!found) return replySyntax(message, `${PREFIX}${command} [@user]`);
      target = found;
    }
    const user = getEcoUser(message.guild.id, target.id);
    const embed = new EmbedBuilder()
      .setColor(0xff8a00)
      .setTitle(`💰 ${target.displayName}'s Balance`)
      .setDescription(`**${formatBucks(user.balance)}** Smoke Bucks`)
      .addFields(
        { name: 'Total Earned', value: formatBucks(user.totalEarned), inline: true },
        { name: 'Gambling Wins', value: formatBucks(user.wins), inline: true },
        { name: 'Gambling Losses', value: formatBucks(user.losses), inline: true },
      );
    return message.reply({ embeds: [embed], allowedMentions: { parse: [] } });
  }

  if (command === 'daily') {
    const user = getEcoUser(message.guild.id, message.author.id);
    const now = Date.now();
    if (user.lastDaily && now - user.lastDaily < DAILY_COOLDOWN_MS) {
      return message.reply({ embeds: [economyErrorEmbed('Daily Already Claimed', `Try again in **${timeLeft(DAILY_COOLDOWN_MS - (now - user.lastDaily))}**.`)] });
    }
    if (!user.lastDaily || now - user.lastDaily > DAILY_COOLDOWN_MS * 2) user.dailyStreak = 0;
    user.dailyStreak = (user.dailyStreak || 0) + 1;
    user.lastDaily = now;
    const reward = DAILY_REWARD + Math.min(user.dailyStreak * 100, 2500);
    user.balance += reward;
    user.totalEarned += reward;
    saveDb();
    await progressQuest(message.guild, message.member, 'use_daily', 1, message.channel.id);
    return message.reply({ embeds: [premiumEmbed('🎁 Daily Claimed', `You received **${formatBucks(reward)}** Smoke Bucks.\n🔥 Streak: **${user.dailyStreak}**`)] });
  }

  if (command === 'work') {
    const user = getEcoUser(message.guild.id, message.author.id);
    const now = Date.now();
    if (user.lastWork && now - user.lastWork < WORK_COOLDOWN_MS) {
      return message.reply({ embeds: [economyErrorEmbed('Work Cooldown', `Try again in **${timeLeft(WORK_COOLDOWN_MS - (now - user.lastWork))}**.`)] });
    }
    user.lastWork = now;
    const reward = randInt(WORK_MIN, WORK_MAX);
    const jobs = ['checked vanities', 'managed tickets', 'boosted activity', 'sold a rare vanity', 'helped the community'];
    user.balance += reward;
    user.totalEarned += reward;
    saveDb();
    await progressQuest(message.guild, message.member, 'use_work', 1, message.channel.id);
    return message.reply({ embeds: [premiumEmbed('💼 Work Complete', `You ${jobs[randInt(0, jobs.length - 1)]} and earned **${formatBucks(reward)}** Smoke Bucks.`)] });
  }

  if (command === 'beg') {
    const user = getEcoUser(message.guild.id, message.author.id);
    const now = Date.now();
    if (user.lastBeg && now - user.lastBeg < BEG_COOLDOWN_MS) {
      return message.reply({ embeds: [economyErrorEmbed('Beg Cooldown', `Try again in **${timeLeft(BEG_COOLDOWN_MS - (now - user.lastBeg))}**.`)] });
    }
    user.lastBeg = now;
    if (Math.random() < 0.18) {
      saveDb();
      return message.reply({ embeds: [premiumEmbed('🥀 No Luck', 'Nobody gave you any Smoke Bucks this time.')] });
    }
    const reward = randInt(BEG_MIN, BEG_MAX);
    user.balance += reward;
    user.totalEarned += reward;
    saveDb();
    return message.reply({ embeds: [premiumEmbed('🤝 Someone Helped You', `You got **${formatBucks(reward)}** Smoke Bucks.`)] });
  }

  if (command === 'donate' || command === 'pay') {
    const target = await getTargetMember(message, args[0]);
    const giver = getEcoUser(message.guild.id, message.author.id);
    const amount = Math.floor(Number(String(args[1] || '').replace(/,/g, '')));
    if (!target || !Number.isFinite(amount) || amount <= 0) return replySyntax(message, `${PREFIX}${command} @user amount`);
    if (target.id === message.author.id) return message.reply({ embeds: [economyErrorEmbed('Invalid Donation', 'You cannot donate to yourself.')] });
    if (target.user.bot) return message.reply({ embeds: [economyErrorEmbed('Invalid Donation', 'You cannot donate to bots.')] });
    const now = Date.now();
    if (!giver.donateWindowStart || now - giver.donateWindowStart > DAILY_COOLDOWN_MS) {
      giver.donateWindowStart = now;
      giver.donatedToday = 0;
    }
    if ((giver.donatedToday || 0) + amount > DONATE_DAILY_LIMIT) {
      return message.reply({ embeds: [economyErrorEmbed('Donate Limit', `You can donate up to **${formatBucks(DONATE_DAILY_LIMIT)}** Smoke Bucks per day.`)] });
    }
    if (giver.balance < amount) return message.reply({ embeds: [economyErrorEmbed('Not Enough Smoke Bucks', `You only have **${formatBucks(giver.balance)}** Smoke Bucks.`)] });
    const receiver = getEcoUser(message.guild.id, target.id);
    giver.balance -= amount;
    giver.donatedToday = (giver.donatedToday || 0) + amount;
    receiver.balance += amount;
    receiver.totalEarned += amount;
    saveDb();
    await progressQuest(message.guild, message.member, 'donate', 1, message.channel.id);
    return message.reply({ embeds: [premiumEmbed('💸 Donation Sent', `${message.author} donated **${formatBucks(amount)}** Smoke Bucks to ${target}.`)] });
  }

  if (command === 'leaderboard' || command === 'lb' || command === 'baltop') {
    const data = getGuildData(message.guild.id);
    const rows = Object.entries(data.economy.users || {})
      .sort((a, b) => (b[1].balance || 0) - (a[1].balance || 0))
      .slice(0, 10);
    if (!rows.length) return message.reply({ embeds: [premiumEmbed('🏆 Smoke Bucks Leaderboard', 'No economy data yet.')] });
    const desc = rows.map(([id, u], i) => `**${i + 1}.** <@${id}> — **${formatBucks(u.balance)}**`).join('\n');
    return message.reply({ embeds: [premiumEmbed('🏆 Smoke Bucks Leaderboard', desc)], allowedMentions: { parse: [] } });
  }

  if (command === 'coinflip' || command === 'cf') {
    const choice = (args[1] || '').toLowerCase();
    if (!['heads', 'tails', 'h', 't'].includes(choice)) return replySyntax(message, `${PREFIX}${command} amount heads/tails`);
    const parsed = betOrSyntax(message, args[0], `${PREFIX}${command} amount heads/tails`);
    if (parsed.error) return parsed.error();
    const pick = choice.startsWith('h') ? 'heads' : 'tails';
    const result = Math.random() < 0.5 ? 'heads' : 'tails';
    const win = pick === result;
    addBalance(message.guild.id, message.author.id, win ? parsed.bet : -parsed.bet);
    const eco = getEcoUser(message.guild.id, message.author.id);
    win ? eco.wins++ : eco.losses++;
    saveDb();
    await progressQuest(message.guild, message.member, 'wager', parsed.bet, message.channel.id);
    if (win) { await progressQuest(message.guild, message.member, 'win_coinflip', 1, message.channel.id); await progressQuest(message.guild, message.member, 'win_gamble', 1, message.channel.id); }
    const frames = [
      { embeds: [premiumEmbed('🪙 Coinflip', [`**${message.author.username}** called **${pick.toUpperCase()}**.`, '', '```', 'Preparing the coin...', '```'].join('\n'))] },
      { embeds: [premiumEmbed('🪙 Coinflip', ['The coin launches into the air... ✨', '', '```', '     🪙', '```'].join('\n'))] },
      { embeds: [premiumEmbed('🪙 Coinflip', ['The coin is spinning fast...', '', '```', '  🪙  ↻  🪙', '```'].join('\n'))] },
      { embeds: [premiumEmbed('🪙 Coinflip', ['It starts wobbling down...', '', '```', '       🪙', '    ↯', '```'].join('\n'))] },
      { embeds: [premiumEmbed('🪙 Coinflip', ['Landing...', '', '```', '   ? ? ?', '```'].join('\n'))] },
    ];
    const final = premiumEmbed(win ? '🪙 Coinflip — You Won' : '🪙 Coinflip — You Lost', [
      `You picked **${pick}**.`,
      `It landed on **${result}**.`,
      '',
      win ? `✅ You won **${formatBucks(parsed.bet)}** Smoke Bucks.` : `❌ You lost **${formatBucks(parsed.bet)}** Smoke Bucks.`,
    ].join('\n'));
    return animatedReply(message, frames, { embeds: [final], allowedMentions: { parse: [] } }, 950);
  }

  if (command === 'slots' || command === 'slot') {
    const parsed = betOrSyntax(message, args[0], `${PREFIX}${command} amount`);
    if (parsed.error) return parsed.error();
    const icons = ['🍒', '🍋', '🍇', '🔔', '💎', '7️⃣'];
    const roll = [icons[randInt(0, icons.length - 1)], icons[randInt(0, icons.length - 1)], icons[randInt(0, icons.length - 1)]];
    let multiplier = 0;
    if (roll[0] === roll[1] && roll[1] === roll[2]) multiplier = roll[0] === '7️⃣' ? 10 : 5;
    else if (roll[0] === roll[1] || roll[1] === roll[2] || roll[0] === roll[2]) multiplier = 1.5;
    const change = multiplier > 0 ? Math.floor(parsed.bet * multiplier) : -parsed.bet;
    addBalance(message.guild.id, message.author.id, change);
    const eco = getEcoUser(message.guild.id, message.author.id);
    change > 0 ? eco.wins++ : eco.losses++;
    saveDb();
    await progressQuest(message.guild, message.member, 'play_slots', 1, message.channel.id);
    await progressQuest(message.guild, message.member, 'wager', parsed.bet, message.channel.id);
    if (change > 0) await progressQuest(message.guild, message.member, 'win_gamble', 1, message.channel.id);
    const frames = [
      { embeds: [premiumEmbed('🎰 Slots', `\`\`\`[ ❔ | ❔ | ❔ ]\`\`\`
Pulling the lever...`)] },
      { embeds: [premiumEmbed('🎰 Slots', `\`\`\`[ 🔄 | 🔄 | 🔄 ]\`\`\`
Reels are spinning... ✨`)] },
      { embeds: [premiumEmbed('🎰 Slots', `\`\`\`[ ${roll[0]} | 🔄 | 🔄 ]\`\`\`
First reel locked...`)] },
      { embeds: [premiumEmbed('🎰 Slots', `\`\`\`[ ${roll[0]} | ${roll[1]} | 🔄 ]\`\`\`
Second reel locked...`)] },
      { embeds: [premiumEmbed('🎰 Slots', `\`\`\`[ ${roll[0]} | ${roll[1]} | ❔ ]\`\`\`
Final reel slowing down...`)] },
    ];
    const final = premiumEmbed(change > 0 ? '🎰 Slots — Jackpot Hit' : '🎰 Slots — No Win', [
      `\`\`\`[ ${roll[0]} | ${roll[1]} | ${roll[2]} ]\`\`\``,
      change > 0 ? `✅ You won **${formatBucks(change)}** Smoke Bucks.
Multiplier: **${multiplier}x**` : `❌ You lost **${formatBucks(parsed.bet)}** Smoke Bucks.`,
    ].join('\n'));
    return animatedReply(message, frames, { embeds: [final] }, 900);
  }

  if (command === 'dice') {
    const choice = (args[1] || '').toLowerCase();
    if (!['over', 'under'].includes(choice)) return replySyntax(message, `${PREFIX}dice amount over/under`);
    const parsed = betOrSyntax(message, args[0], `${PREFIX}dice amount over/under`);
    if (parsed.error) return parsed.error();
    const roll = randInt(1, 100);
    const win = choice === 'over' ? roll > 50 : roll < 50;
    addBalance(message.guild.id, message.author.id, win ? parsed.bet : -parsed.bet);
    const eco = getEcoUser(message.guild.id, message.author.id);
    win ? eco.wins++ : eco.losses++;
    saveDb();
    await progressQuest(message.guild, message.member, 'play_dice', 1, message.channel.id);
    await progressQuest(message.guild, message.member, 'wager', parsed.bet, message.channel.id);
    if (win) await progressQuest(message.guild, message.member, 'win_gamble', 1, message.channel.id);
    const frames = [
      { embeds: [premiumEmbed('🎲 Dice', [`Choice: **${choice} 50**`, '', '```', 'Shaking the dice...', '```'].join('\n'))] },
      { embeds: [premiumEmbed('🎲 Dice', ['The dice is bouncing... ✨', '', '```', '🎲  →  🎲', '```'].join('\n'))] },
      { embeds: [premiumEmbed('🎲 Dice', ['Still rolling...', '', '```', '   🎲', '      🎲', '```'].join('\n'))] },
      { embeds: [premiumEmbed('🎲 Dice', ['Final bounce...', '', '```', 'Result loading...', '```'].join('\n'))] },
    ];
    const final = premiumEmbed(win ? '🎲 Dice — You Won' : '🎲 Dice — You Lost', [
      `Rolled **${roll}**.`,
      `You chose **${choice} 50**.`,
      '',
      win ? `✅ You won **${formatBucks(parsed.bet)}** Smoke Bucks.` : `❌ You lost **${formatBucks(parsed.bet)}** Smoke Bucks.`,
    ].join('\n'));
    return animatedReply(message, frames, { embeds: [final] }, 900);
  }

  if (command === 'roulette' || command === 'rl') {
    const choice = (args[1] || '').toLowerCase();
    if (!['red', 'black', 'green'].includes(choice)) return replySyntax(message, `${PREFIX}${command} amount red/black/green`);
    const parsed = betOrSyntax(message, args[0], `${PREFIX}${command} amount red/black/green`);
    if (parsed.error) return parsed.error();
    const n = randInt(0, 36);
    const color = n === 0 ? 'green' : n % 2 === 0 ? 'black' : 'red';
    const colorEmoji = color === 'red' ? '🔴' : color === 'black' ? '⚫' : '🟢';
    const win = choice === color;
    const payout = choice === 'green' ? parsed.bet * 14 : parsed.bet;
    addBalance(message.guild.id, message.author.id, win ? payout : -parsed.bet);
    const eco = getEcoUser(message.guild.id, message.author.id);
    win ? eco.wins++ : eco.losses++;
    saveDb();
    await progressQuest(message.guild, message.member, 'wager', parsed.bet, message.channel.id);
    if (win) { await progressQuest(message.guild, message.member, 'win_roulette', 1, message.channel.id); await progressQuest(message.guild, message.member, 'win_gamble', 1, message.channel.id); }
    const frames = [
      { embeds: [premiumEmbed('🎡 Roulette', [`Bet: **${choice}**`, '', '```', 'Wheel spinning...', '```'].join('\n'))] },
      { embeds: [premiumEmbed('🎡 Roulette', ['The ball drops onto the wheel... ✨', '', '```', '◉  3  17  22  9  0  31', '```'].join('\n'))] },
      { embeds: [premiumEmbed('🎡 Roulette', ['The ball is circling fast...', '', '```', '12  ◉  28  5  19  34  7', '```'].join('\n'))] },
      { embeds: [premiumEmbed('🎡 Roulette', ['The ball is slowing down...', '', '```', '8  23  ◉  10  30  1  14', '```'].join('\n'))] },
      { embeds: [premiumEmbed('🎡 Roulette', ['Almost there...', '', '```', '?  ?  ◉  ?  ?', '```'].join('\n'))] },
    ];
    const final = premiumEmbed(win ? '🎡 Roulette — You Won' : '🎡 Roulette — You Lost', [
      `Landed on **${n} ${colorEmoji} ${color}**.`,
      '',
      win ? `✅ You won **${formatBucks(payout)}** Smoke Bucks.` : `❌ You lost **${formatBucks(parsed.bet)}** Smoke Bucks.`,
    ].join('\n'));
    return animatedReply(message, frames, { embeds: [final] }, 1000);
  }

  if (command === 'blackjack' || command === 'bj') {
    return startInteractiveBlackjack(message, command, args);
  }

  if (command === 'tictactoe' || command === 'ttt') {
    return startTicTacToe(message, command, args);
  }

  return false;
}

// =========================
// COMMAND HANDLER
// =========================
async function handleCommand(message) {
  const raw = message.content.slice(PREFIX.length).trim();
  if (!raw) return;

  const args = raw.split(/\s+/);
  const command = (args.shift() || '').toLowerCase();

  if (command === 'help') {
    const embed = new EmbedBuilder()
      .setTitle('Bot Commands')
      .setDescription([
        `Prefix: \`${PREFIX}\``,
        '',
        '**UwUify**',
        `\`${PREFIX}uwuify @user\` - Target a user`,
        `\`${PREFIX}unuwuify @user\` - Remove a user`,
        `\`${PREFIX}uwulist\` - Show targeted users`,
        '',
        '**VoiceMaster**',
        `\`${PREFIX}voicemaster setup\` - Create VoiceMaster channels`,
        `\`${PREFIX}vm setup\` - Same as above`,
        `\`${PREFIX}vc help\` - Show VC controls`,
        '',
        '**Moderation**',
        `\`${PREFIX}purge 50\` - Delete messages quickly`,
        '',
        '**Economy**',
        `\`${PREFIX}economy help\` - Show Smoke Bucks commands`,
        `\`${PREFIX}quests\` - View daily missions`,
      ].join('\n'));
    return message.reply({ embeds: [embed] });
  }

  if (command === 'uwuify') {
    if (!hasUwUPerm(message.member)) {
      return message.reply('You need Manage Messages, Manage Server, or Administrator to use this.');
    }

    const target = await getTargetMember(message, args[0]);
    if (!target) return replySyntax(message, `${PREFIX}uwuify @user`);
    if (target.user.bot) return message.reply('Bots cannot be uwuified.');

    const data = getGuildData(message.guild.id);
    if (!data.uwuTargets.includes(target.id)) data.uwuTargets.push(target.id);
    saveDb();

    return message.reply(`${target} is now uwuified.`);
  }

  if (command === 'unuwuify' || command === 'removeuwu') {
    if (!hasUwUPerm(message.member)) {
      return message.reply('You need Manage Messages, Manage Server, or Administrator to use this.');
    }

    const target = await getTargetMember(message, args[0]);
    if (!target) return replySyntax(message, `${PREFIX}unuwuify @user`);

    const data = getGuildData(message.guild.id);
    data.uwuTargets = data.uwuTargets.filter((id) => id !== target.id);
    saveDb();

    return message.reply(`${target} is no longer uwuified.`);
  }

  if (command === 'uwulist') {
    const data = getGuildData(message.guild.id);
    if (!data.uwuTargets.length) return message.reply('No users are currently uwuified.');

    const list = data.uwuTargets.map((id, index) => `${index + 1}. <@${id}>`).join('\n');
    return message.reply({
      embeds: [new EmbedBuilder().setTitle('UwUified Users').setDescription(list)],
      allowedMentions: { parse: [] },
    });
  }

  if (command === 'purge' || command === 'clear') {
    if (!message.member.permissions.has(PermissionsBitField.Flags.ManageMessages) && !message.member.permissions.has(PermissionsBitField.Flags.Administrator)) {
      return message.reply('You need Manage Messages or Administrator to use this.');
    }
    const amount = Number.parseInt(args[0], 10);
    if (Number.isNaN(amount) || amount < 1 || amount > 100) {
      return replySyntax(message, `${PREFIX}${command} amount`, 'Amount must be between 1 and 100. Example: `*purge 50`');
    }
    try {
      await message.delete().catch(() => null);
      const messages = await message.channel.messages.fetch({ limit: amount });
      await message.channel.bulkDelete(messages, true);
    } catch (error) {
      console.error('Purge error:', error);
      return message.reply({ embeds: [economyErrorEmbed('Purge Failed', 'I could not delete those messages. Make sure they are not older than 14 days and I have Manage Messages.')] });
    }
    return;
  }

  const economyCommands = ['economy', 'eco', 'balance', 'bal', 'daily', 'work', 'beg', 'donate', 'pay', 'leaderboard', 'lb', 'baltop', 'coinflip', 'cf', 'slots', 'slot', 'dice', 'roulette', 'rl', 'blackjack', 'bj', 'tictactoe', 'ttt', 'quests', 'missions', 'dailyquests'];
  if (economyCommands.includes(command)) {
    return handleEconomyCommand(message, command, args);
  }

  if (command === 'voicemaster' || command === 'vm') {
    const sub = (args.shift() || '').toLowerCase();
    if (sub === 'setup') return setupVoiceMaster(message);
    return replySyntax(message, `${PREFIX}${command} setup`);
  }

  if (command === 'vc') {
    return handleVcCommand(message, args);
  }
}

// =========================
// EVENTS
// =========================
client.once('clientReady', () => {
  console.log(`Logged in as ${client.user.tag}`);
  console.log('Package: premium-games-quests-v6-fixed');
  console.log(`Prefix: ${PREFIX}`);
  console.log(`Data file: ${DATA_FILE}`);
  setInterval(() => tickVcQuests().catch((error) => console.error('Quest VC tick error:', error)), 60_000);
});

client.on('messageCreate', async (message) => {
  try {
    if (!message.guild || message.author.bot) return;

    rememberLastTextChannel(message);

    // Created VC chats are bot-info only. Users cannot chat there.
    if (isTempVcChat(message.channel)) {
      await message.delete().catch(() => null);
      return;
    }

    if (message.content.startsWith(PREFIX)) {
      await handleCommand(message);
      return;
    }

    await progressQuest(message.guild, message.member, 'chat', 1, message.channel.id);
    await handleUwUMessage(message);
  } catch (error) {
    console.error('messageCreate error:', error);
  }
});

client.on('voiceStateUpdate', async (oldState, newState) => {
  try {
    await handleVoiceStateUpdate(oldState, newState);
  } catch (error) {
    console.error('voiceStateUpdate error:', error);
  }
});

process.on('unhandledRejection', (error) => {
  console.error('Unhandled promise rejection:', error);
});

process.on('uncaughtException', (error) => {
  console.error('Uncaught exception:', error);
});

client.login(TOKEN);
